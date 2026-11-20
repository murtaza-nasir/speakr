"""Limits on the recording metadata a client may write (#624).

Every route that writes title, participants, notes, summary or
meeting_date checks the body here first, so the API and the web app refuse
the same values, and SQLite and PostgreSQL answer alike: participants is a VARCHAR(500), which
SQLite stores past its width and PostgreSQL refuses at flush.

The title has no limit here: the model clips it to 200 characters
(Recording._clip_title). Notes and summary are TEXT columns; their limit
is a bound on what a client may store, far above a long meeting summary
or note and far below a pasted file. The summarizer reads at most 8000
characters of notes (SUMMARY_NOTES_LIMIT), but notes are also for the
reader, so the storage limit is not tied to that prompt budget.
"""

from src.utils.localization import translate

PARTICIPANTS_MAX_CHARS = 500
NOTES_MAX_CHARS = 200_000
SUMMARY_MAX_CHARS = 200_000

LIMITS = {'title': None,
          'participants': PARTICIPANTS_MAX_CHARS,
          'notes': NOTES_MAX_CHARS,
          'summary': SUMMARY_MAX_CHARS,
          # A date as an ISO string, or null to clear it; the route parses
          # the string (#655).
          'meeting_date': None}


def metadata_error(data, current=None):
    """The 400 body for the first metadata field of `data` that cannot be
    stored, or None when every field can.

    `data` is the parsed JSON body; a value that is not an object is
    refused as a whole. A field is a string or null. A value equal to the one in `current`
    (field -> stored value) is accepted as is: the web app sends every
    field on each save, and a stored value written by the pipeline (a long
    participant list from many speakers) must not block a save of notes.
    """
    if not isinstance(data, dict):
        return {'error': translate('server.apiError.body_must_be_object'),
                'code': 'body_must_be_object'}
    current = current or {}
    for field, limit in LIMITS.items():
        if field not in data:
            continue
        value = data[field]
        if value is None or (field in current and value == current[field]):
            continue
        if not isinstance(value, str):
            return {'error': translate('server.apiError.field_must_be_string', field=field),
                    'code': 'invalid_parameter'}
        if limit is not None and len(value) > limit:
            return {'error': translate('server.apiError.field_too_long', field=field, max=limit),
                    'code': 'invalid_parameter'}
    return None
