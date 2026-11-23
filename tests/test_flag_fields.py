"""Flag fields are parsed, never cast with bool() (#665, #674).

One lenient reading for every flag: true/false, "true"/"false" (any case)
and 0/1. Anything else answers 400 invalid_boolean_flag before the first
change, so a refused request changes nothing.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
from src.utils.request_body import parse_flag
from tests.request_body_support import code_of, make_world, send

FALSE_VALUES = [False, "false", "False", " FALSE ", 0, "0"]
TRUE_VALUES = [True, "true", "TRUE", 1, "1"]
BAD_VALUES = [None, 2, -1, "yes", "", [], {}, 1.0]


@pytest.fixture
def world():
    with make_world() as ids:
        yield ids


@pytest.mark.parametrize("value", FALSE_VALUES)
def test_parse_flag_reads_false(value):
    assert parse_flag(value) is False


@pytest.mark.parametrize("value", TRUE_VALUES)
def test_parse_flag_reads_true(value):
    assert parse_flag(value) is True


@pytest.mark.parametrize("value", BAD_VALUES)
def test_parse_flag_refuses_anything_else(value):
    assert parse_flag(value) is None


def _rec(world):
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        return r.is_inbox, r.is_highlighted, r.is_archived


def _set(world, **values):
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        for k, v in values.items():
            setattr(r, k, v)
        db.session.commit()


@pytest.mark.parametrize("field", ["is_inbox", "is_highlighted", "is_archived"])
def test_single_patch_stores_the_string_false_as_false(world, field):
    _set(world, is_inbox=True, is_highlighted=True, is_archived=True)
    resp = send(world, "PATCH", f"/api/v1/recordings/{world['rec']}", json_body={field: "false"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["recording"][field] is False


@pytest.mark.parametrize("field", ["is_inbox", "is_highlighted", "is_archived"])
@pytest.mark.parametrize("bad", [None, "yes", 2, []], ids=repr)
def test_single_patch_refuses_a_value_that_is_not_a_flag_before_any_change(world, field, bad):
    _set(world, is_inbox=True, is_highlighted=False, is_archived=False)
    resp = send(world, "PATCH", f"/api/v1/recordings/{world['rec']}",
                json_body={"title": "Changed", "is_inbox": False, field: bad})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_boolean_flag"
    assert field in resp.get_json()["error"]
    assert _rec(world) == (True, False, False)
    with app.app_context():
        assert db.session.get(Recording, world["rec"]).title == "Sync"


@pytest.mark.parametrize("field", ["is_inbox", "is_highlighted", "is_archived"])
def test_batch_patch_reads_flags_the_same_way(world, field):
    _set(world, is_inbox=True, is_highlighted=True, is_archived=True)
    resp = send(world, "PATCH", "/api/v1/recordings/batch",
                json_body={"recording_ids": [world["rec"]], "updates": {field: "false"}})
    assert resp.status_code == 200 and resp.get_json()["updated"] == 1
    assert _rec(world)[["is_inbox", "is_highlighted", "is_archived"].index(field)] is False
    resp = send(world, "PATCH", "/api/v1/recordings/batch",
                json_body={"recording_ids": [world["rec"]], "updates": {field: "nope"}})
    assert resp.status_code == 400 and code_of(resp) == "invalid_boolean_flag"


@pytest.mark.parametrize("value,expected", [("false", False), ("true", True), (0, False), (True, True)])
def test_auto_summarization_reads_the_flag(world, value, expected):
    resp = send(world, "PUT", "/api/v1/settings/auto-summarization", json_body={"enabled": value})
    assert resp.status_code == 200
    assert resp.get_json()["auto_summarization"] is expected
    with app.app_context():
        assert db.session.get(User, world["owner"]).auto_summarization is expected


@pytest.mark.parametrize("body", [{"enabled": None}, {"enabled": "off"}, {"enabled": 5}])
def test_auto_summarization_refuses_a_value_that_is_not_a_flag(world, body):
    with app.app_context():
        before = db.session.get(User, world["owner"]).auto_summarization
    resp = send(world, "PUT", "/api/v1/settings/auto-summarization", json_body=body)
    assert resp.status_code == 400 and code_of(resp) == "invalid_boolean_flag"
    with app.app_context():
        assert db.session.get(User, world["owner"]).auto_summarization == before


@pytest.mark.parametrize("body", [[1], "x"])
def test_auto_summarization_refuses_a_body_that_is_not_an_object(world, body):
    resp = send(world, "PUT", "/api/v1/settings/auto-summarization", json_body=body)
    assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"


@pytest.mark.parametrize("field,column", [("inbox", "is_inbox"), ("highlight", "is_highlighted"),
                                          ("archive", "is_archived")])
def test_web_bulk_toggle_reads_the_string_false_as_false(world, field, column):
    _set(world, is_inbox=True, is_highlighted=True, is_archived=True)
    resp = send(world, "POST", "/api/recordings/bulk-toggle",
                json_body={"recording_ids": [world["rec"]], "field": field, "value": "false"})
    assert resp.status_code == 200
    assert _rec(world)[["is_inbox", "is_highlighted", "is_archived"].index(column)] is False


@pytest.mark.parametrize("bad", ["maybe", 2, [], {}])
def test_web_bulk_toggle_refuses_a_value_that_is_not_a_flag(world, bad):
    _set(world, is_inbox=True, is_highlighted=False, is_archived=False)
    resp = send(world, "POST", "/api/recordings/bulk-toggle",
                json_body={"recording_ids": [world["rec"]], "field": "inbox", "value": bad})
    assert resp.status_code == 400 and code_of(resp) == "invalid_boolean_flag"
    assert _rec(world) == (True, False, False)


def test_a_key_placeholder_reaches_the_message():
    """translate() and api_error() take the catalog key positionally, so a
    {{key}} placeholder can be filled (it was a TypeError, a 500)."""
    from src.utils.localization import api_error, translate
    with app.test_request_context():
        assert translate("server.apiError.invalid_boolean_flag", key="force_new") == "force_new must be true or false"
        assert api_error("invalid_boolean_flag", key="value")["error"] == "value must be true or false"
