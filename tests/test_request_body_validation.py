"""A JSON body the client sent wrong is a 400 or 415, never a 500 (#653).

An empty body answers 400 no_data_provided, malformed JSON 400
invalid_json, a body sent as text/plain 415 invalid_json, and valid JSON
that is not an object 400 body_must_be_object. None of them is logged as
an unexpected error, and no answer quotes the parser.

The source check at the end keeps every POST, PUT and PATCH route from
reading the body raw inside a broad try.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import ast
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from werkzeug.exceptions import BadRequest, NotFound, UnsupportedMediaType

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app
from src.utils.request_body import (BodyNotObject, bool_field, is_int, is_int_list, json_body,
                                    json_object_body)
from src.utils.route_errors import internal_error
from tests.request_body_support import code_of, make_world, send

API = Path(__file__).resolve().parent.parent / "src" / "api"

# (method, path); {rec}, {a}, {speaker} and {group} are filled from the world
REQUIRED = [
    ("POST", "/recording/{rec}/download/chat"),
    ("POST", "/recording/{rec}/update_speakers"),
    ("POST", "/api/recordings/incognito/chat"),
    ("POST", "/api/recordings/incognito/summary"),
    ("POST", "/api/recordings/batch-status"),
    ("POST", "/chat"),
    ("POST", "/api/recordings/{rec}/tags"),
    ("PUT", "/api/recordings/{rec}/tags/reorder"),
    ("DELETE", "/api/recordings/bulk"),
    ("POST", "/api/recordings/bulk-tags"),
    ("POST", "/api/recordings/bulk-reprocess"),
    ("POST", "/api/recordings/bulk-toggle"),
    ("POST", "/save"),
    ("POST", "/api/inquire/sessions"),
    ("POST", "/api/inquire/search"),
    ("POST", "/api/inquire/chat"),
    ("POST", "/speakers"),
    ("PUT", "/speakers/{speaker}"),
    ("POST", "/speakers/merge"),
    ("POST", "/api/recordings/{rec}/share-internal"),
    ("POST", "/api/push/subscribe"),
    ("POST", "/api/push/unsubscribe"),
    ("PUT", "/api/v1/recordings/{rec}/speakers/assign"),
    ("PATCH", "/api/v1/recordings/{rec}"),
    ("PUT", "/api/v1/recordings/{rec}/notes"),
    ("PUT", "/api/v1/recordings/{rec}/summary"),
    ("POST", "/api/v1/tags"),
    ("PUT", "/api/v1/tags/{a}"),
    ("POST", "/api/v1/speakers"),
    ("PUT", "/api/v1/speakers/{speaker}"),
    ("POST", "/api/tags"),
    ("PUT", "/api/tags/{a}"),
    ("POST", "/api/groups/{group}/tags"),
    ("POST", "/api/naming-templates"),
    ("POST", "/api/export-templates"),
    ("POST", "/api/initial-prompt-templates"),
    ("POST", "/api/transcript-templates"),
    ("POST", "/api/user/preferences"),
]

# Routes where every field is optional: no body is {} and reaches the
# route's own check (code), a body that is present is parsed strictly.
OPTIONAL = [
    ("POST", "/recording/{rec}/update_transcript", "invalid_transcript_data"),
    ("POST", "/recording/{rec}/update_transcription", "no_transcription_data"),
    ("POST", "/recording/{rec}/reprocess_transcription", None),
    ("POST", "/api/v1/recordings/{rec}/tags", "tag_ids_required"),
]

# Routes whose errors go through internal_error (admin, not edited here)
ADMIN = [
    ("POST", "/admin/transcription/visible-models"),
    ("POST", "/admin/inquire/process-recordings"),
    ("POST", "/admin/auto-process/config"),
]

# A value that is not an object answers the route's own code here
NON_OBJECT_CODE = {"/api/recordings/{rec}/share-internal": "user_id_required"}


@pytest.fixture
def world():
    with make_world() as ids, \
            patch("src.api.inquire.ENABLE_INQUIRE_MODE", True), \
            patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True, create=True), \
            patch("src.api.shares.ENABLE_INTERNAL_SHARING", True, create=True), \
            patch("src.api.tags.ENABLE_INTERNAL_SHARING", True, create=True), \
            patch("src.app.ENABLE_INTERNAL_SHARING", True, create=True), \
            patch("src.api.push_notifications._get_vapid_config",
                  return_value={"enabled": True, "public_key": "k", "private_key": "p"}):
        yield ids


def _path(world, path):
    return path.format(rec=world["rec"], a=world["a"], speaker=world["speaker"], group=world["group"])


def _no_unexpected_error(caplog):
    assert not [r for r in caplog.records if "Unexpected error" in r.getMessage()]


def _no_parser_text(resp):
    text = resp.get_data(as_text=True)
    assert "Failed to decode" not in text and "Bad Request" not in text and "Traceback" not in text


BAD_BODIES = [
    ("", "application/json", 400, "no_data_provided"),
    ("{not json", "application/json", 400, "invalid_json"),
    ('{"x": 1}', "text/plain", 415, "invalid_json"),
    ('{"x": 1}', None, 415, "invalid_json"),
]


@pytest.mark.parametrize("method,path", REQUIRED)
@pytest.mark.parametrize("data,ctype,status,code", BAD_BODIES)
def test_an_unusable_body_is_a_client_error(world, caplog, method, path, data, ctype, status, code):
    caplog.set_level(logging.ERROR)
    resp = send(world, method, _path(world, path), data=data, content_type=ctype)
    assert resp.status_code == status, resp.get_data(as_text=True)[:300]
    assert code_of(resp) == code
    _no_parser_text(resp)
    _no_unexpected_error(caplog)


@pytest.mark.parametrize("method,path", REQUIRED)
@pytest.mark.parametrize("value", [[1], 42, True, "x"])
def test_a_body_that_is_not_an_object_is_refused(world, caplog, method, path, value):
    caplog.set_level(logging.ERROR)
    resp = send(world, method, _path(world, path), json_body=value)
    assert resp.status_code == 400, resp.get_data(as_text=True)[:300]
    assert code_of(resp) == NON_OBJECT_CODE.get(path, "body_must_be_object")
    _no_unexpected_error(caplog)


@pytest.mark.parametrize("method,path,empty_code", OPTIONAL)
def test_an_optional_body_is_parsed_strictly_when_present(world, caplog, method, path, empty_code):
    caplog.set_level(logging.ERROR)
    for data, ctype, status, code in BAD_BODIES[1:]:
        resp = send(world, method, _path(world, path), data=data, content_type=ctype)
        assert resp.status_code == status, (data, resp.get_data(as_text=True)[:300])
        assert code_of(resp) == code
    for value in ([1], "x", 42):
        resp = send(world, method, _path(world, path), json_body=value)
        assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"
    if empty_code:
        for data, ctype in (("", "application/json"), ("", None)):
            resp = send(world, method, _path(world, path), data=data, content_type=ctype)
            assert resp.status_code == 400 and code_of(resp) == empty_code
    _no_unexpected_error(caplog)


@pytest.mark.parametrize("method,path", ADMIN)
@pytest.mark.parametrize("data,ctype,status,code", BAD_BODIES)
def test_admin_parse_errors_keep_their_status(world, caplog, method, path, data, ctype, status, code):
    caplog.set_level(logging.ERROR)
    resp = send(world, method, path, data=data, content_type=ctype, who="admin")
    assert resp.status_code == status, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body["code"] == code and "error_id" not in body
    _no_unexpected_error(caplog)


# ------------------------------------------------------------- positive

def test_valid_bodies_still_reach_the_routes(world):
    rec = world["rec"]
    resp = send(world, "POST", "/api/recordings/batch-status", json_body={"recording_ids": [rec]})
    assert resp.status_code == 200
    resp = send(world, "POST", "/api/recordings/batch-status", json_body={})
    assert resp.status_code == 400 and code_of(resp) == "recording_ids_required"
    resp = send(world, "POST", "/speakers", json_body={"name": f"Omar {world['suffix']}"})
    assert resp.status_code in (200, 201)
    resp = send(world, "POST", "/api/v1/tags", json_body={"name": f"new-{world['suffix']}"})
    assert resp.status_code == 201
    resp = send(world, "PUT", f"/api/v1/recordings/{rec}/notes", json_body={"notes": "n"})
    assert resp.status_code == 200 and resp.get_json()["notes"] == "n"
    resp = send(world, "POST", "/admin/auto-process/config", json_body={"x": 1}, who="admin")
    assert resp.status_code == 200 and resp.get_json()["success"] is True


def test_update_transcript_accepts_a_valid_object(world):
    with patch("src.api.recordings.reindex_recording_chunks_async"), patch("src.api.recordings.export_recording"):
        resp = send(world, "POST", f"/recording/{world['rec']}/update_transcript",
                    json_body={"transcript_data": [{"speaker": "SPEAKER_00", "sentence": "Edited.",
                                                    "start_time": 0.0, "end_time": 1.0}]})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    resp = send(world, "POST", f"/recording/{world['rec']}/update_transcript", json_body={"transcript_data": []})
    assert resp.status_code == 400 and code_of(resp) == "invalid_transcript_data"


# -------------------------------------------------------------- helpers

def _ctx(data, content_type="application/json"):
    kwargs = {"method": "POST", "data": data}
    if content_type:
        kwargs["content_type"] = content_type
    return app.test_request_context("/x", **kwargs)


@pytest.mark.parametrize("data,ctype,expected", [
    ("", "application/json", (400, "no_data_provided")),
    ("   ", "application/json", (400, "no_data_provided")),
    ("{bad", "application/json", (400, "invalid_json")),
    ('{"a": 1}', "text/plain", (415, "invalid_json")),
    ("[1]", "application/json", (400, "body_must_be_object")),
    ("null", "application/json", (400, "body_must_be_object")),
])
def test_json_object_body_refusals(data, ctype, expected):
    with _ctx(data, ctype):
        value, error = json_object_body()
        assert value is None
        resp, status = error
        assert (status, resp.get_json()["code"]) == expected


def test_json_object_body_accepts_an_object_and_optional_bodies():
    with _ctx('{"a": 1}'):
        assert json_object_body() == ({"a": 1}, None)
    for data, ctype in (("", None), ("", "application/json"), ("null", "application/json")):
        with _ctx(data, ctype):
            assert json_object_body(optional=True) == ({}, None)
    with _ctx("[1]"):
        assert json_object_body(optional=True)[1][1] == 400


def test_json_body_returns_any_json_value():
    for raw, value in (("[]", []), ("0", 0), ('"x"', "x"), ("false", False), ("null", None)):
        with _ctx(raw):
            assert json_body() == (value, None)


def test_field_type_helpers():
    assert is_int(3) and not is_int(True) and not is_int("3") and not is_int(2.5)
    assert is_int_list([]) and is_int_list([1, 2])
    for bad in (5, "ab", {"a": 1}, [True], ["3"], [None], [[1]], [{}], [1.5]):
        assert not is_int_list(bad), bad
    with app.test_request_context("/"):
        assert bool_field({}, "is_default", False, "invalid_is_default") == (False, None)
        assert bool_field({"is_default": True}, "is_default", False, "invalid_is_default") == (True, None)
        for bad in ("false", 1, 0, None, []):
            value, (resp, status) = bool_field({"is_default": bad}, "is_default", False, "invalid_is_default")
            assert status == 400 and resp.get_json()["code"] == "invalid_is_default"


def test_internal_error_keeps_the_parse_status(caplog):
    caplog.set_level(logging.ERROR)
    with _ctx("{bad"):
        resp, status = internal_error(BadRequest())
        assert status == 400 and resp.get_json()["code"] == "invalid_json"
    with _ctx('{"a": 1}', "text/plain"):
        resp, status = internal_error(UnsupportedMediaType())
        assert status == 415 and resp.get_json()["code"] == "invalid_json"
    with _ctx("[1]"):
        resp, status = internal_error(BodyNotObject())
        assert status == 400 and resp.get_json()["code"] == "body_must_be_object"
    _no_unexpected_error(caplog)
    with _ctx('{"a": 1}'):
        resp, status = internal_error(RuntimeError("boom"))
        assert status == 500 and resp.get_json()["code"] == "unexpected_error_ref"
        assert resp.get_json()["error_id"]
        # Other HTTP errors and an explicit status keep the reference body
        resp, status = internal_error(NotFound())
        assert status == 500 and resp.get_json()["code"] == "unexpected_error_ref"
        resp, status = internal_error(BadRequest(), status=503)
        assert status == 503 and resp.get_json()["code"] == "unexpected_error_ref"


# ------------------------------------------------------------ source check

_WRITE = {"POST", "PUT", "PATCH", "DELETE"}


def _route_methods(fn):
    methods = set()
    for d in fn.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
            if d.func.attr == "route":
                for k in d.keywords:
                    if k.arg == "methods" and isinstance(k.value, (ast.List, ast.Tuple)):
                        methods |= {e.value for e in k.value.elts if isinstance(e, ast.Constant)}
            elif d.func.attr in ("post", "put", "patch", "delete"):
                methods.add(d.func.attr.upper())
    return methods


def _raw_read(node):
    """request.json, or request.get_json() without silent=True."""
    if isinstance(node, ast.Attribute) and node.attr == "json" and getattr(node.value, "id", None) == "request":
        return True
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get_json"
            and getattr(node.func.value, "id", None) == "request"):
        return not any(k.arg == "silent" and getattr(k.value, "value", None) is True for k in node.keywords)
    return False


def _broad(handler):
    names = []
    t = handler.type
    if t is None:
        return True
    for e in (t.elts if isinstance(t, ast.Tuple) else [t]):
        names.append(getattr(e, "id", getattr(e, "attr", "")))
    return "Exception" in names or "BaseException" in names


def _catches_http_first(try_node):
    for h in try_node.handlers:
        t = h.type
        for e in (t.elts if isinstance(t, ast.Tuple) else [t]) if t is not None else []:
            if getattr(e, "id", getattr(e, "attr", "")) in ("HTTPException", "BadRequest"):
                return True
        if _broad(h):
            return False
    return False


def test_no_write_route_reads_the_body_raw_inside_a_broad_try():
    """Flask raises BadRequest or UnsupportedMediaType from request.json and
    request.get_json(); inside `try: ... except Exception` that became a 500.
    Routes read the body through src/utils/request_body.py instead.

    The admin routes reach internal_error, which keeps the parse status, so
    they are allowed to read raw (src/api/admin.py is edited elsewhere)."""
    found = []
    for path in sorted(API.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or not (_route_methods(fn) & _WRITE):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Try) or not any(_broad(h) for h in node.handlers):
                    continue
                if _catches_http_first(node):
                    continue
                uses_internal_error = any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "internal_error"
                                          for h in node.handlers for c in ast.walk(h))
                for stmt in node.body:
                    for sub in ast.walk(stmt):
                        if _raw_read(sub) and not uses_internal_error:
                            found.append(f"{path.name}:{sub.lineno} {fn.name}")
    assert found == []
