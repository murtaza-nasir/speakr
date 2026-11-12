"""Which recordings still need transcription, a summary, or speaker labels.

The paginated recordings list offers three "needs" filters, and the SQL for
them used to be built inside the recordings blueprint. The policy lives here
so that code outside the blueprint can ask the same question without importing
a route module, and so that it can be tested against a database without a
Flask client.

Every predicate returns a SQLAlchemy expression over ``Recording``:

- ``needs_transcription_condition``: idle recordings whose transcript is
  blank (NULL or whitespace-only) or is itself an error message.
- ``needs_summary_condition``: idle recordings with a valid transcript whose
  summary is blank or carries a failure/skip marker.
- ``needs_speakers_condition``: idle recordings with a valid transcript that
  still contains the literal ``SPEAKER_`` token, i.e. structured speaker
  labels that have not been replaced with names.

"Idle" means a status outside :data:`ACTIVE_RECORDING_STATUSES`; a NULL
status counts as idle, for rows written before the column existed. "Blank"
means NULL or ``trim(column) = ''``, so stray spaces do not hide a recording
from the filter. Prefix matching escapes LIKE metacharacters, so the ``_`` in
``ERROR_JSON:`` and in the summary markers matches literally rather than as a
wildcard.
"""

from src.database import db
from src.models import Recording
from src.utils.error_formatting import TRANSCRIPTION_ERROR_PREFIXES
from src.utils.search import LIKE_ESCAPE, escape_like

ACTIVE_RECORDING_STATUSES = ['QUEUED', 'PENDING', 'PROCESSING', 'SUMMARIZING']
SUMMARY_NEEDS_PREFIXES = [
    '[Summary generation failed:',
    '[Summary skipped:',
    '[Summary not generated]',
]


def idle_recording_condition():
    """Recordings that are not currently queued or running work."""
    return db.or_(
        Recording.status.is_(None),
        ~Recording.status.in_(ACTIVE_RECORDING_STATUSES),
    )


def _blank_column_condition(column):
    return db.or_(column.is_(None), db.func.trim(column) == '')


def _prefixed_column_condition(column, prefixes):
    # Escape LIKE metacharacters in the literal prefixes so a '_' or '%' in a
    # prefix (e.g. 'ERROR_JSON:') matches literally instead of as a wildcard.
    return db.or_(*[
        column.like(f'{escape_like(prefix)}%', escape=LIKE_ESCAPE)
        for prefix in prefixes
    ])


def transcription_error_condition():
    """Transcripts that are an error message rather than content."""
    return _prefixed_column_condition(Recording.transcription, TRANSCRIPTION_ERROR_PREFIXES)


def valid_transcription_condition():
    """Transcripts that have content and are not an error message."""
    return db.and_(
        ~_blank_column_condition(Recording.transcription),
        ~transcription_error_condition(),
    )


def needs_transcription_condition():
    """Idle recordings with no transcript or a failed one."""
    return db.and_(
        idle_recording_condition(),
        db.or_(
            _blank_column_condition(Recording.transcription),
            transcription_error_condition(),
        ),
    )


def needs_summary_condition():
    """Idle recordings with a valid transcript and no usable summary."""
    return db.and_(
        idle_recording_condition(),
        valid_transcription_condition(),
        db.or_(
            _blank_column_condition(Recording.summary),
            _prefixed_column_condition(Recording.summary, SUMMARY_NEEDS_PREFIXES),
        ),
    )


def needs_speakers_condition():
    """Idle recordings whose transcript still carries SPEAKER_ labels."""
    return db.and_(
        idle_recording_condition(),
        valid_transcription_condition(),
        Recording.transcription.like('%SPEAKER\\_%', escape='\\'),
    )
