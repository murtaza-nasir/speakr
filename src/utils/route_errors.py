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
    database session is rolled back first.

    The 400 and 415 that Flask raises for an unusable JSON body are the
    client's mistake, not a fault (#653): they keep their status, answer
    invalid_json (or no_data_provided, body_must_be_object) and are not
    logged as errors. An explicit status still forces the reference body."""
    try:
        from src.database import db
        db.session.rollback()
    except Exception:
        pass
    if status == 500:
        from src.utils.request_body import parse_error_response
        answer = parse_error_response(exc)
        if answer is not None:
            return answer
    return jsonify(unexpected_error_body(exc)), status
