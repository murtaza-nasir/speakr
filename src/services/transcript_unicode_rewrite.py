"""One-time rewrite of transcripts stored with \\uXXXX escapes (#622).

Before #622 every JSON transcript was written with json.dumps(...) and its
default ensure_ascii=True, so "español" is stored as "espa\\u00f1ol" in
recording.transcription. New writes use ensure_ascii=False. This rewrite
stores the old rows once in the same form, so that the text a LIKE search
reads is the text a user typed.

A row is rewritten only when its transcription parses as a JSON array or
object and the new text parses to the same value. Plain-text transcripts,
rows that fail to parse and rows whose new text could not be stored as UTF-8
(a lone surrogate escape) are left as they are. The search in
src/utils/search.py still matches escaped rows, for rows restored from a
backup taken before the rewrite.

The UPDATE is a core statement on the table, not an ORM change: the
before_flush listener in src/services/recording_changes.py would move
updated_at and send recording.updated webhooks, and the content a client sees
is the same before and after. It also requires the transcription to be
unchanged since it was read, so a transcript saved by the app in the meantime
is never overwritten.

Rows are read in windows of BATCH_SIZE ids and each window is committed on
its own. An interrupted run is resumed at the next start: a rewritten row no
longer matches the escape filter, and a row that matches but needs no change
is left alone. The setting DONE_KEY marks the end of a complete pass. The
log holds counts only, never a title, file name or any transcript text.
"""

import json
import logging

from sqlalchemy import text

DONE_KEY = 'transcript_unicode_rewrite_done'
BATCH_SIZE = 200

# '!' as the escape character, so the backslash in the pattern is a literal
# backslash on every backend (PostgreSQL reads a backslash as the default
# escape of LIKE, SQLite has no default one).
_ESCAPE_FILTER = "transcription LIKE :pattern ESCAPE '!'"
_PATTERN = '%\\u%'

logger = logging.getLogger(__name__)


def rewritten_text(stored):
    """The raw-Unicode form of `stored`, or None when the row must be kept.

    None for text that is not a JSON array or object, for text that fails to
    parse, when nothing changes, and when the new text does not parse to the
    same value or cannot be encoded as UTF-8.
    """
    if not stored or '\\u' not in stored:
        return None
    try:
        parsed = json.loads(stored)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, (list, dict)):
        return None
    new = json.dumps(parsed, ensure_ascii=False)
    if new == stored:
        return None
    try:
        new.encode('utf-8')
        if json.loads(new) != parsed:
            return None
    except (ValueError, UnicodeEncodeError):
        return None
    return new


def _is_done():
    from src.models import SystemSetting
    return SystemSetting.query.filter_by(key=DONE_KEY).first() is not None


def run_transcript_unicode_rewrite(engine=None, batch_size=BATCH_SIZE, log=None):
    """Rewrite every escaped JSON transcript once; needs an app context.

    Returns the counts, or None when an earlier run completed the rewrite.
    """
    from src.database import db
    from src.models import SystemSetting

    log = log or logger
    engine = engine or db.engine
    if _is_done():
        return None

    counts = {'scanned': 0, 'rewritten': 0, 'skipped': 0, 'changed_meanwhile': 0}
    with engine.connect() as conn:
        max_id = conn.execute(text('SELECT MAX(id) FROM recording')).scalar() or 0

    low = 0
    while low < max_id:
        high = low + batch_size
        with engine.begin() as conn:
            rows = conn.execute(
                text(f'SELECT id, transcription FROM recording '
                     f'WHERE id > :low AND id <= :high AND {_ESCAPE_FILTER} ORDER BY id'),
                {'low': low, 'high': high, 'pattern': _PATTERN}).fetchall()
            for rec_id, stored in rows:
                counts['scanned'] += 1
                new = rewritten_text(stored)
                if new is None:
                    counts['skipped'] += 1
                    continue
                result = conn.execute(
                    text('UPDATE recording SET transcription = :new '
                         'WHERE id = :id AND transcription = :old'),
                    {'new': new, 'id': rec_id, 'old': stored})
                if result.rowcount:
                    counts['rewritten'] += 1
                else:
                    counts['changed_meanwhile'] += 1
        rows = None
        low = high

    SystemSetting.set_setting(
        DONE_KEY, 'true',
        'Transcripts stored with \\u escapes were rewritten as raw Unicode (#622)')
    log.info(
        "Transcript Unicode rewrite (#622) complete: %d with escapes, %d rewritten, "
        "%d left as they are, %d changed during the run",
        counts['scanned'], counts['rewritten'], counts['skipped'], counts['changed_meanwhile'])
    return counts
