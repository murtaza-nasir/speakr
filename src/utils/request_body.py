"""The JSON body of a request, checked at the route boundary.

A body the client sent wrong is a client error, never a server fault. The
helpers here turn each way a body can be wrong into a 400 or 415 answer in
the API error shape (#653, #654, #656):

- no body at all: 400 no_data_provided (or {} where the body is optional);
- a body sent with a content type other than JSON: 415 invalid_json;
- a body that is not valid JSON: 400 invalid_json;
- valid JSON that is not an object where an object is required:
  400 body_must_be_object.

The answer never quotes the parser, so no exception text reaches the
client. The type checks at the end are for fields inside an object.
"""

from flask import jsonify, request
from werkzeug.exceptions import BadRequest, HTTPException, UnsupportedMediaType

from src.utils.localization import api_error


class BodyNotObject(BadRequest):
    """The body parsed to a JSON value that is not an object."""

    description = "The request body must be a JSON object."


def _answer(code, status):
    return jsonify(api_error(code)), status


def _has_body():
    return bool(request.get_data(cache=True).strip())


def json_body():
    """(value, None) for the parsed JSON value of the request, which may be
    any JSON type, or (None, error_response) when the body is missing, has
    a content type other than JSON, or is not valid JSON."""
    if not _has_body():
        return None, _answer('no_data_provided', 400)
    if not request.is_json:
        return None, _answer('invalid_json', 415)
    try:
        return request.get_json(), None
    except BadRequest:
        return None, _answer('invalid_json', 400)


def json_object_body(optional=False):
    """(dict, None) for the JSON object of the request, or
    (None, error_response) for a body that is missing, unparsable or not an
    object.

    With optional=True a request without a body (or with the body null)
    gives {}, for routes where every field is optional; a body that is
    present is still parsed strictly.
    """
    if optional and not _has_body():
        return {}, None
    data, error = json_body()
    if error:
        return None, error
    if optional and data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, _answer('body_must_be_object', 400)
    return data, None


def optional_json_object():
    """The JSON object of the request, or {} for a request without a body.

    For routes where every field is optional. A body that is present is
    still parsed strictly: malformed JSON gives 400 and a content type other
    than JSON gives 415, so fields a client sent are never dropped silently.
    A JSON value that is not an object raises BodyNotObject (400).
    """
    if not request.get_data(cache=True):
        return {}
    data = request.get_json()
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BodyNotObject()
    return data


def parse_error_response(exc):
    """The client-error answer for `exc` when it is the 400 or 415 that
    Flask raises for an unusable JSON body, else None."""
    if not isinstance(exc, HTTPException) or exc.code not in (400, 415):
        return None
    if isinstance(exc, BodyNotObject):
        return _answer('body_must_be_object', 400)
    # The length header, not the body: a form route may have read the
    # stream already, and the body itself is not needed here.
    if not request.content_length and 'chunked' not in request.headers.get('Transfer-Encoding', ''):
        return _answer('no_data_provided', 400)
    if isinstance(exc, UnsupportedMediaType):
        return _answer('invalid_json', 415)
    return _answer('invalid_json', 400)


# ------------------------------------------------------------ field types

def is_int(value):
    """True for a JSON integer; a boolean is not one."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_int_list(value):
    """True for a JSON array whose items are all integers (booleans,
    strings, floats, null, arrays and objects are refused)."""
    return isinstance(value, list) and all(is_int(item) for item in value)


def bool_field(data, field, default, code):
    """(value, None) for the JSON boolean at data[field], or default when
    the field is absent; (None, error_response) with `code` for any other
    type ("false", 1, 0, null, [] are refused, not read as a flag)."""
    if field not in data:
        return default, None
    value = data[field]
    if not isinstance(value, bool):
        return None, _answer(code, 400)
    return value, None
