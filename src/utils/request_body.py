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

# The largest value of an Integer column: INT4 on PostgreSQL, so every id
# column holds at most 2**31 - 1. A larger id names no row; looking one up
# raises OverflowError (SQLite past 2**63, PostgreSQL past 2**31) instead
# of finding nothing (#664, #676, #677). The app's <int:...> URL converter
# and the id checks below share this bound.
DB_INT_MAX = 2 ** 31 - 1
DB_INT_MIN = -2 ** 31


def is_int(value):
    """True for a JSON integer; a boolean is not one."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_db_id(value):
    """True for a JSON integer an Integer column can hold, so a lookup by
    it finds a row or nothing, never an overflow."""
    return is_int(value) and DB_INT_MIN <= value <= DB_INT_MAX


def is_int_list(value):
    """True for a JSON array of ids: integers in the range of an Integer
    column (booleans, strings, floats, null, arrays, objects and integers
    past that range are refused)."""
    return isinstance(value, list) and all(is_db_id(item) for item in value)


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


_FLAG_WORDS = {'true': True, '1': True, 'false': False, '0': False}


def parse_flag(value):
    """The boolean of a flag field read leniently, or None when the value
    is not a flag: true/false, 0/1 and the strings "true", "false", "1",
    "0" (any case, surrounding spaces ignored). bool("false") is True, so
    a string is parsed, never cast; null, 2, "yes", [] and {} are not
    flags (#665, #674)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return _FLAG_WORDS.get(value.strip().lower())
    return None


def flag_fields(data, fields):
    """({field: bool}, None) for each of `fields` present in data, parsed
    with parse_flag; (None, error_response) 400 invalid_boolean_flag that
    names the first field that is not a flag. Checked as a whole, so a
    route refuses the request before its first write."""
    flags = {}
    for field in fields:
        if field in data:
            value = parse_flag(data[field])
            if value is None:
                return None, (jsonify(api_error('invalid_boolean_flag', key=field)), 400)
            flags[field] = value
    return flags, None


def str_field(data, field, code, required=True):
    """(value, None) for the string at data[field], stripped of
    surrounding spaces; (None, error_response) 400 with `code` when the
    value is not a string (null included, #680) or, with required=True,
    when the field is absent or blank. An absent optional field gives
    (None, None)."""
    if field not in data:
        return (None, _answer(code, 400)) if required else (None, None)
    value = data[field]
    if not isinstance(value, str):
        return None, _answer(code, 400)
    value = value.strip()
    if required and not value:
        return None, _answer(code, 400)
    return value, None


def id_fields_error(data, fields, code='invalid_integer_value'):
    """The 400 answer with `code` for the first of `fields` in data that
    is not an optional id, else None. An optional id is null, '' (none),
    an integer in the column range or a string of its digits ("3", which
    the web forms send); true, 2.5, [3], {} and ids past the range are
    refused before any lookup by them (#664, #687)."""
    for field in fields:
        value = data.get(field)
        if value is None or value == '':
            continue
        if isinstance(value, str) and value.strip().isdecimal():
            value = int(value.strip())
        if not is_db_id(value):
            return _answer(code, 400)
    return None


# ------------------------------------------------------------ query string

def id_args_error(names, code='invalid_integer_value'):
    """The 400 answer with `code` when one of the query-string arguments
    `names` is a whole number past the range of an Integer column, else
    None. Values that are not whole numbers are left to the route, which
    reads them as before (most ignore them); only an id that would
    overflow the lookup is refused here (#664)."""
    for name in names:
        raw = request.args.get(name)
        if raw is None:
            continue
        for part in raw.split(','):
            try:
                value = int(part)
            except ValueError:
                continue
            if not DB_INT_MIN <= value <= DB_INT_MAX:
                return _answer(code, 400)
    return None


# ------------------------------------------------------------ field specs

def clean_fields(data, spec):
    """Check the fields of `spec` present in data and normalise them in
    place; None when every one is usable, else the 400 answer for the
    first that is not. A spec entry is one of:

    - ('str', max_length or None): a string or null; field_must_be_string,
      field_too_long;
    - ('int', low, high): a whole number from low to high, its digits as a
      string, or null/'' (stored as null); field_out_of_range;
    - ('flag',): parse_flag; invalid_boolean_flag.

    For the processing fields of folders and tags (#687), whose columns are
    String(n), Integer and Boolean."""
    for field, rule in spec.items():
        if field not in data:
            continue
        value = data[field]
        kind = rule[0]
        if kind == 'str':
            if value is None:
                continue
            if not isinstance(value, str):
                return jsonify(api_error('field_must_be_string', field=field)), 400
            if rule[1] is not None and len(value.strip()) > rule[1]:
                return jsonify(api_error('field_too_long', field=field, max=rule[1])), 400
        elif kind == 'int':
            low, high = rule[1], rule[2]
            if value is None or value == '':
                data[field] = None
                continue
            if isinstance(value, str) and value.strip().lstrip('-').isdecimal():
                value = int(value.strip())
            if not is_int(value) or not low <= value <= high:
                return jsonify(api_error('field_out_of_range', field=field, min=low, max=high)), 400
            data[field] = value
        elif kind == 'flag':
            parsed = parse_flag(value)
            if parsed is None:
                return jsonify(api_error('invalid_boolean_flag', key=field)), 400
            data[field] = parsed
    return None


def int_args_error(bounds):
    """The 400 answer field_out_of_range for the first query-string
    argument of `bounds` ({name: (low, high)}) that is a whole number
    outside its range, else None. For counts such as days, months and page
    sizes, where a huge number would overflow a date or an offset (#664).
    Values that are not whole numbers are left to the route."""
    for name, (low, high) in bounds.items():
        raw = request.args.get(name)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if not low <= value <= high:
            return jsonify(api_error('field_out_of_range', field=name, min=low, max=high)), 400
    return None


# The processing fields of a folder or a tag, for clean_fields: the columns
# are Text, String(10), String(120), Integer and Boolean (#687).
PROCESSING_FIELDS = {
    'custom_prompt': ('str', None),
    'title_prompt': ('str', None),
    'default_language': ('str', 10),
    'default_min_speakers': ('int', 0, 100),
    'default_max_speakers': ('int', 0, 100),
    'default_hotwords': ('str', None),
    'default_initial_prompt': ('str', None),
    'default_transcription_model': ('str', 120),
    # -1 keeps the recordings forever (protected from auto-deletion)
    'retention_days': ('int', -1, 36500),
    'protect_from_deletion': ('flag',),
    'auto_share_on_apply': ('flag',),
    'share_with_group_lead': ('flag',),
    'is_auto_process': ('flag',),
}
