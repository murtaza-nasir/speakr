#!/usr/bin/env python3
"""Backfill `recording.audio_duration_seconds` for existing recordings.

Updates only rows where `audio_duration_seconds IS NULL`, skips deleted audio and
active processing states, and reads audio via the storage facade (local/S3).
"""

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
from src.utils.ffprobe import get_duration  # noqa: E402

SKIP_STATUSES = {'PROCESSING', 'QUEUED'}


def parse_args():
    parser = argparse.ArgumentParser(description='Backfill recording.audio_duration_seconds using storage layer + ffprobe')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--recording-id', type=int, default=None)
    parser.add_argument('--only-user', type=int, default=None)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--report-jsonl', type=str, default=None)
    parser.add_argument('--ffprobe-timeout', type=int, default=30)
    return parser.parse_args()


def _report(fp, *, recording_id, action, duration=None, audio_path=None, error=None):
    if not fp:
        return
    row = {
        'ts': datetime.utcnow().isoformat(),
        'recording_id': recording_id,
        'action': action,
        'duration': duration,
        'audio_path': audio_path,
        'error': error,
    }
    fp.write(json.dumps(row, ensure_ascii=False) + '\n')
    fp.flush()


def main() -> int:
    args = parse_args()
    storage = get_storage_service()
    report_fp = open(args.report_jsonl, 'a', encoding='utf-8') if args.report_jsonl else None

    stats = {
        'scanned': 0,
        'updated': 0,
        'skipped_status': 0,
        'skipped_audio_deleted': 0,
        'skipped_no_audio_path': 0,
        'skipped_already_set': 0,
        'skipped_missing_object': 0,
        'skipped_no_duration': 0,
        'errors': 0,
    }

    try:
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

                if recording.audio_duration_seconds is not None:
                    stats['skipped_already_set'] += 1
                    continue

                if recording.status in SKIP_STATUSES:
                    stats['skipped_status'] += 1
                    _report(report_fp, recording_id=recording.id, action='skip_status', audio_path=recording.audio_path)
                    continue

                if recording.audio_deleted_at is not None:
                    stats['skipped_audio_deleted'] += 1
                    _report(report_fp, recording_id=recording.id, action='skip_audio_deleted', audio_path=recording.audio_path)
                    continue

                if not recording.audio_path:
                    stats['skipped_no_audio_path'] += 1
                    _report(report_fp, recording_id=recording.id, action='skip_no_audio_path')
                    continue

                try:
                    if not storage.exists(recording.audio_path):
                        stats['skipped_missing_object'] += 1
                        _report(report_fp, recording_id=recording.id, action='skip_missing_object', audio_path=recording.audio_path)
                        continue

                    with storage.materialize(recording.audio_path) as materialized:
                        duration = get_duration(materialized.local_path, timeout=args.ffprobe_timeout)

                    if duration is None:
                        stats['skipped_no_duration'] += 1
                        _report(report_fp, recording_id=recording.id, action='skip_no_duration', audio_path=recording.audio_path)
                        continue

                    _report(report_fp, recording_id=recording.id, action='update', duration=float(duration), audio_path=recording.audio_path)
                    if not args.dry_run:
                        recording.audio_duration_seconds = float(duration)
                        db.session.add(recording)
                        db.session.commit()
                    stats['updated'] += 1
                except Exception as exc:
                    db.session.rollback()
                    stats['errors'] += 1
                    _report(report_fp, recording_id=recording.id, action='error', audio_path=recording.audio_path, error=str(exc))
    finally:
        if report_fp:
            report_fp.close()

    print(json.dumps({'timestamp': datetime.utcnow().isoformat(), **stats}, ensure_ascii=False, indent=2))
    return 0 if stats['errors'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
