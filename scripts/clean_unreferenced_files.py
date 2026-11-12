#!/usr/bin/env python3
"""List, and optionally delete, media files that no database row refers to.

Older releases could leave a recording's file in the uploads folder after the
recording was deleted. This command finds such files. By default it is a dry
run: nothing is changed. See docs/admin-guide/retention.md
("Files Without a Recording").

Usage inside the container:

    docker compose exec app python scripts/clean_unreferenced_files.py
    docker compose exec app python scripts/clean_unreferenced_files.py --list
    docker compose exec app python scripts/clean_unreferenced_files.py --delete
    docker compose exec app python scripts/clean_unreferenced_files.py --move-to /data/quarantine
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

PREVIEW_ROWS = 20


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description='Find media files in storage that no recording or upload part refers to. '
                    'Dry run unless --delete or --move-to is given.')
    action = p.add_mutually_exclusive_group()
    action.add_argument('--delete', action='store_true', help='Delete the unreferenced files')
    action.add_argument('--move-to', metavar='DIR',
                        help='Move the unreferenced files to DIR (local storage only), keeping their relative paths')
    p.add_argument('--yes', action='store_true', help='Do not ask for confirmation')
    p.add_argument('--min-age-hours', type=float, default=48.0,
                   help='Leave files changed within this many hours alone (default 48)')
    p.add_argument('--list', action='store_true', help='List every unreferenced file and every row whose file is missing')
    args = p.parse_args(argv)
    if args.min_age_hours < 0:
        p.error('--min-age-hours must not be negative')
    return args


def _print_report(result, args, fmt):
    print(f"Storage: {'S3 bucket ' + result.s3_bucket + ' and ' if result.s3_bucket else ''}"
          f"local folder {result.root or '(none)'}")
    for d in result.scanned_dirs:
        print(f"  Scanned: {d}")
    if result.s3_bucket:
        print(f"  Scanned: s3://{result.s3_bucket}/{result.s3_prefix}")
    if result.not_scanned:
        print(f"  Not scanned: {', '.join(result.not_scanned)}")
    print(f"Files looked at: {result.scanned}, referenced by a row: {result.referenced}")
    print(f"Unreferenced: {len(result.candidates)} file(s), {fmt(result.total_bytes)}")
    print(f"Skipped, changed within {args.min_age_hours:g} h: {result.recent} file(s), {fmt(result.recent_bytes)}")
    if result.skipped_links:
        print(f"Skipped, symbolic link or outside the uploads folder: {len(result.skipped_links)}")
    if result.kept_name_matches:
        print(f"Kept, same name as a row whose file is missing: {len(result.kept_name_matches)}")
        for path in result.kept_name_matches[:PREVIEW_ROWS if not args.list else None]:
            print(f"  {path}")

    shown = result.candidates if args.list else result.candidates[:PREVIEW_ROWS]
    if shown:
        print('')
        print('Unreferenced files (changed, size, path):')
        for c in shown:
            print(f"  {c.modified:%Y-%m-%d %H:%M}  {fmt(c.size):>9}  {c.display}")
        if len(shown) < len(result.candidates):
            print(f"  ... {len(result.candidates) - len(shown)} more; use --list to see all")

    print('')
    print(f"Rows whose file is missing (not changed): {len(result.missing_rows)}")
    rows = result.missing_rows if args.list else result.missing_rows[:PREVIEW_ROWS]
    for row in rows:
        print(f"  {row.table} {row.row_id}: {row.locator}")
    if len(rows) < len(result.missing_rows):
        print(f"  ... {len(result.missing_rows) - len(rows)} more; use --list to see all")


def _import_app():
    """Import the Flask app without starting any background work.

    Importing src.app runs the startup tasks. Without the flag the import could
    win the job-queue election and recover (re-queue) jobs the running server
    is working on, or start the schedulers. The flag is read only during the
    import, so it is removed again afterwards.
    """
    if 'src.app' in sys.modules:
        return sys.modules['src.app'].app
    previous = os.environ.get('SPEAKR_SKIP_BACKGROUND_TASKS')
    os.environ['SPEAKR_SKIP_BACKGROUND_TASKS'] = 'true'
    try:
        from src.app import app
    finally:
        if previous is None:
            os.environ.pop('SPEAKR_SKIP_BACKGROUND_TASKS', None)
        else:
            os.environ['SPEAKR_SKIP_BACKGROUND_TASKS'] = previous
    return app


def main(argv=None, *, storage=None, input_fn=input) -> int:
    args = parse_args(argv)

    app = _import_app()
    from src.services import unreferenced_files as uf

    min_age = timedelta(hours=args.min_age_hours)
    with app.app_context():
        if storage is None:
            from src.services.storage import get_storage_service
            storage = get_storage_service()
        if args.move_to:
            try:
                uf.validate_move_target(args.move_to, storage)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 2
        try:
            result = uf.scan(storage, min_age=min_age)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2

        _print_report(result, args, uf.format_size)

        if not (args.delete or args.move_to):
            print('')
            print('Dry run: nothing was changed. Use --delete to delete these files '
                  'or --move-to DIR to move them.')
            return 0
        if not result.candidates:
            print('')
            print('Nothing to do.')
            return 0
        if args.move_to and any(c.backend == 's3' for c in result.candidates):
            print('Error: --move-to supports local files only; S3 objects can only be deleted.',
                  file=sys.stderr)
            return 2

        verb = f"Move {len(result.candidates)} file(s) to {args.move_to}" if args.move_to \
            else f"Delete {len(result.candidates)} file(s)"
        if not args.yes:
            try:
                answer = input_fn(f"{verb}, {uf.format_size(result.total_bytes)}? Type 'yes' to continue: ")
            except EOFError:
                answer = ''
            if answer.strip().lower() != 'yes':
                print('Cancelled: nothing was changed.')
                return 1

        outcome = uf.apply(result.candidates, storage, min_age=min_age, move_to=args.move_to)
        done = 'Moved' if args.move_to else 'Deleted'
        print('')
        print(f"{done}: {outcome.removed} file(s), {uf.format_size(outcome.removed_bytes)}")
        for line in outcome.skipped:
            print(f"  Skipped {line}")
        for line in outcome.errors:
            print(f"  Error {line}")
        return 1 if outcome.errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
