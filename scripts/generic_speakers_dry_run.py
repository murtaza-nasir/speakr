#!/usr/bin/env python3
"""Report what the diarization-label cleanup removes, without changing anything.

On the first start after the upgrade, Speakr removes diarization labels
(SPEAKER_00, "Speaker 1", "Unknown Speaker") from the stores that hold
people: saved speakers with their voice samples and snippets, participants,
the speaker name of Inquire chunks and stored Ask filters. Transcripts keep
their labels. See docs/admin-guide/privacy.md ("Diarization labels").

This command prints the counts of that cleanup for a database. It only
reads: a SQLite file is opened read-only, and no statement that writes is
sent to any database. The output holds counts and ids, never a name.

A second part counts the recordings whose stored transcript holds
diarization labels in another form than SPEAKER_NN ("A", "S01",
"SPEAKER_0"), and how those labels are treated today. New transcripts are
normalized when they are stored; stored transcripts are not rewritten. See
src/utils/unnormalized_label_report.py. --ids adds the recording ids.

Usage inside the container (the database of the instance):

    docker compose exec app python scripts/generic_speakers_dry_run.py

Against a copy, or another database:

    python scripts/generic_speakers_dry_run.py --database-uri sqlite:////path/to/copy.db
    python scripts/generic_speakers_dry_run.py --database-uri postgresql://user:password@host/dbname
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description='Print the counts of the diarization-label cleanup. Reads only; changes nothing.')
    p.add_argument('--database-uri',
                   help='Database to read (default: SQLALCHEMY_DATABASE_URI, as the application uses)')
    p.add_argument('--ids', action='store_true',
                   help='List the ids of the recordings whose labels are not in the normalized form')
    return p.parse_args(argv)


def read_only_engine(uri):
    """An engine that cannot write to a SQLite file; other databases are only read from."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    url = make_url(uri)
    if url.get_backend_name() == 'sqlite':
        if not url.database or url.database == ':memory:':
            raise SystemExit('Give the path of a SQLite file.')
        if not os.path.exists(url.database):
            raise SystemExit(f'No such file: {url.database}')
        import sqlite3
        path = os.path.abspath(url.database)
        return create_engine('sqlite://', creator=lambda: sqlite3.connect(f'file:{path}?mode=ro', uri=True))
    return create_engine(uri)


def main(argv=None):
    args = parse_args(argv)
    uri = args.database_uri or os.environ.get('SQLALCHEMY_DATABASE_URI')
    if not uri:
        raise SystemExit('No database: give --database-uri or set SQLALCHEMY_DATABASE_URI.')

    from src.utils import unnormalized_label_report
    from src.utils.generic_speaker_cleanup import plan, summary_lines
    engine = read_only_engine(uri)
    try:
        planned = plan(engine)
        unnormalized = unnormalized_label_report.report(engine)
    finally:
        engine.dispose()

    print('Diarization-label cleanup, dry run. Nothing was changed.')
    for line in summary_lines(planned['counts'], planned['kept_speaker_ids'],
                              users_affected=planned['users_affected'], dry_run=True):
        print('  ' + line)

    print()
    print('Stored transcripts with diarization labels that are not in the normalized form. Nothing was changed.')
    for line in unnormalized_label_report.summary_lines(unnormalized, with_ids=args.ids):
        print('  ' + line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
