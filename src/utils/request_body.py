"""The JSON body of a request for routes where the body is optional."""

from flask import request
from werkzeug.exceptions import BadRequest


def optional_json_object():
    """The JSON object of the request, or {} for a request without a body.

    For routes where every field is optional. A body that is present is
    still parsed strictly: malformed JSON gives 400 and a content type other
    than JSON gives 415, so fields a client sent are never dropped silently.
    A JSON value that is not an object gives 400.
    """
    if not request.get_data(cache=True):
        return {}
    data = request.get_json()
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BadRequest("The request body must be a JSON object.")
    return data
