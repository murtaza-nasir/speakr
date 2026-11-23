"""The assistant routes check a question the same way (#668, #670, #673).

A question is a string, stripped; one that is blank once stripped is no
question, one that holds a NUL character is refused, and one over
MAX_QUESTION_CHARS is too long. The checks run before any model call, so
no model is needed here. API v1 inquire takes filters only as an object:
a missing or null filters is no filter, [] is refused.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.assistant_limits import MAX_QUESTION_CHARS, checked_question
from tests.request_body_support import code_of, make_world, send

BLANK = ["   ", "\n\t ", "", None, 5, ["hi"]]


@pytest.fixture
def world():
    with make_world() as ids, \
            patch("src.api.inquire.ENABLE_INQUIRE_MODE", True), \
            patch.dict(os.environ, {"ENABLE_INQUIRE_MODE": "true"}):
        yield ids


def test_checked_question_strips_and_refuses():
    missing = {"code": "missing"}
    assert checked_question("  why?  ", missing) == ("why?", None)
    for raw in BLANK:
        assert checked_question(raw, missing) == (None, missing), raw
    _, error = checked_question("a\x00b", missing)
    assert error["code"] == "message_contains_nul"
    _, error = checked_question("a\x00b", missing, v1=True)
    assert error["code"] == "invalid_parameter"
    _, error = checked_question("x" * (MAX_QUESTION_CHARS + 1), missing)
    assert error["code"] == "message_too_long"
    # Surrounding spaces do not count toward the limit
    assert checked_question(" " * 50 + "x" * MAX_QUESTION_CHARS, missing)[1] is None


@pytest.mark.parametrize("message", BLANK, ids=repr)
def test_web_inquire_chat_refuses_a_blank_message(world, message):
    resp = send(world, "POST", "/api/inquire/chat", json_body={"message": message})
    assert resp.status_code == 400 and code_of(resp) == "no_message_provided"


def test_web_inquire_chat_refuses_nul(world):
    resp = send(world, "POST", "/api/inquire/chat", json_body={"message": "a\x00b"})
    assert resp.status_code == 400 and code_of(resp) == "message_contains_nul"


@pytest.mark.parametrize("message", BLANK, ids=repr)
def test_v1_recording_chat_refuses_a_blank_message(world, message):
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/chat", json_body={"message": message})
    assert resp.status_code == 400 and code_of(resp) == "message_required"


def test_v1_recording_chat_refuses_nul_and_a_body_that_is_not_an_object(world):
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/chat", json_body={"message": "a\x00"})
    assert resp.status_code == 400 and code_of(resp) == "invalid_parameter"
    assert "NUL" in resp.get_json()["error"]
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/chat", json_body=["hi"])
    assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"


def test_v1_inquire_refuses_blank_and_nul_questions(world):
    for question in BLANK:
        resp = send(world, "POST", "/api/v1/inquire", json_body={"question": question})
        assert resp.status_code == 400 and code_of(resp) == "invalid_parameter", question
    resp = send(world, "POST", "/api/v1/inquire", json_body={"question": "why\x00"})
    assert resp.status_code == 400 and "NUL" in resp.get_json()["error"]


@pytest.mark.parametrize("filters", [[], "", 0, False, ["tag"], "a"], ids=repr)
def test_v1_inquire_refuses_filters_that_are_not_an_object(world, filters):
    resp = send(world, "POST", "/api/v1/inquire", json_body={"question": "why?", "filters": filters})
    assert resp.status_code == 400, resp.get_data(as_text=True)[:200]
    assert resp.get_json()["error"] == "filters must be an object"


@pytest.mark.parametrize("query", BLANK, ids=repr)
def test_inquire_search_refuses_a_blank_query(world, query):
    resp = send(world, "POST", "/api/inquire/search", json_body={"query": query})
    assert resp.status_code == 400 and code_of(resp) == "no_query_provided"


def test_inquire_search_refuses_a_query_over_the_limit_and_nul(world):
    resp = send(world, "POST", "/api/inquire/search", json_body={"query": "x" * (MAX_QUESTION_CHARS + 1)})
    assert resp.status_code == 400 and code_of(resp) == "message_too_long"
    resp = send(world, "POST", "/api/inquire/search", json_body={"query": "x\x00"})
    assert resp.status_code == 400 and code_of(resp) == "message_contains_nul"


def test_inquire_search_passes_the_stripped_query_on(world):
    seen = {}

    def fake_search(user_id, query, filters, top_k):
        seen["query"] = query
        return []

    with patch("src.api.inquire.semantic_search_chunks", fake_search):
        resp = send(world, "POST", "/api/inquire/search", json_body={"query": "  budget  "})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
    assert seen["query"] == "budget"
