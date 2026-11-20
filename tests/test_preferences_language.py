"""POST /api/user/preferences stores only a shipped interface locale as
`language`, or null (#659). The account form keeps a shipped locale too.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User
from tests.request_body_support import code_of, make_world, send


@pytest.fixture
def world():
    with make_world() as ids:
        yield ids


def _saved_language(world):
    with app.app_context():
        return db.session.get(User, world["owner"]).ui_language


@pytest.mark.parametrize("value", [{"nested": {"deep": "x"}}, {"a": 1}, [1], 123, True, 1.5])
def test_preferences_api_refuses_a_non_string_language(world, value):
    resp = send(world, "POST", "/api/user/preferences", json_body={"language": value})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_ui_language"
    assert _saved_language(world) == "en"


@pytest.mark.parametrize("code", ["not-a-locale", "xx", "<script>", ""])
def test_preferences_api_refuses_an_unknown_language_code(world, code):
    resp = send(world, "POST", "/api/user/preferences", json_body={"language": code})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_ui_language"
    assert _saved_language(world) == "en"


@pytest.mark.parametrize("value,stored", [("de", "de"), ("de-AT", "de"), ("pt", "pt-BR"),
                                          ("ZH", "zh"), (None, None)])
def test_preferences_api_accepts_and_normalizes_a_shipped_language(world, value, stored):
    resp = send(world, "POST", "/api/user/preferences", json_body={"language": value})
    assert resp.status_code == 200
    assert resp.get_json()["ui_language"] == stored
    assert _saved_language(world) == stored


def test_preferences_api_without_language_keeps_it(world):
    resp = send(world, "POST", "/api/user/preferences", json_body={"dark_mode": True})
    assert resp.status_code == 200
    assert _saved_language(world) == "en"
