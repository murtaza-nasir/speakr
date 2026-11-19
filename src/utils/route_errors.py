"""The answer of a route to an error it did not expect.

The text of such an exception can quote SQL, file paths, connection
settings or a provider's answer, so it goes only to the log. The client
gets the translated unexpected_error message with a short reference that
is also in the log line, so an admin can find the cause.
"""

import uuid

from flask import current_app, jsonify, request

from src.utils.localization import api_error


def error_reference():
    return uuid.uuid4().hex[:8]


def log_unexpected(exc, ref):
    """Log `exc` with its traceback, the route and the reference `ref`."""
    try:
        where = f"{request.method} {request.path}"
    except RuntimeError:
        where = "(no request)"
    current_app.logger.error("Unexpected error %s in %s: %r", ref, where, exc,
                             exc_info=(type(exc), exc, exc.__traceback__))


def unexpected_error_body(exc):
    """The error body for `exc`, logged with a new reference."""
    ref = error_reference()
    log_unexpected(exc, ref)
    body = api_error('unexpected_error_ref', ref=ref)
    body['error_id'] = ref
    return body


def internal_error(exc, status=500):
    """The JSON response for an exception a route did not expect; the
    database session is rolled back first."""
    try:
        from src.database import db
        db.session.rollback()
    except Exception:
        pass
    return jsonify(unexpected_error_body(exc)), status
