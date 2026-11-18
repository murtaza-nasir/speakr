"""Limits on what a client may send to the assistant's chat routes.

Every route that answers a question checks the body here first, so the Ask
page (POST /api/inquire/chat), POST /api/v1/inquire, the recording panel
(POST /chat), the incognito chat and POST /api/v1/recordings/<id>/chat refuse
the same input before any model call.

The question (#634) is at most MAX_QUESTION_CHARS characters: len() of the
string, not its UTF-8 bytes. A typed or dictated question is far shorter; a
pasted transcript or document is longer, and the transcript already reaches
the model by other means. A longer question is refused with 400, never cut:
a question that loses its last clause can ask something else.
"""

from src.utils.localization import api_error, format_number, translate

MAX_QUESTION_CHARS = 10_000


def question_too_long(value):
    """True when the question is a string longer than the shared limit."""
    return isinstance(value, str) and len(value) > MAX_QUESTION_CHARS


def question_error(value, v1=False):
    """The 400 body for a question over the limit, or None when it fits.

    The web routes answer the code message_too_long; API v1 answers
    invalid_parameter, as for its other bad parameters. Both say the same
    localized sentence.
    """
    if not question_too_long(value):
        return None
    limit = format_number(MAX_QUESTION_CHARS)
    if v1:
        return {'error': translate('server.apiError.message_too_long', max=limit), 'code': 'invalid_parameter'}
    return api_error('message_too_long', max=limit)
