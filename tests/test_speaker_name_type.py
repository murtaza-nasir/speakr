"""A speaker name that is not a string is a 400, never a 500 (#680).

null, a number, a list or a blank string is refused by the web routes and
API v1 alike, and nothing is stored.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Speaker
from tests.request_body_support import code_of, make_world, send

BAD = [None, 5, ["Dana"], {"n": 1}, "", "   ", True]


@pytest.fixture
def world():
    with make_world() as ids:
        yield ids


def _count(world):
    with app.app_context():
        return Speaker.query.filter_by(user_id=world["owner"]).count()


@pytest.mark.parametrize("name", BAD, ids=repr)
def test_create_refuses_a_name_that_is_not_a_string(world, name):
    before = _count(world)
    for path in ("/speakers", "/api/v1/speakers"):
        resp = send(world, "POST", path, json_body={"name": name})
        assert resp.status_code == 400, (path, resp.status_code)
        assert code_of(resp) == "speaker_name_required", path
    assert _count(world) == before


@pytest.mark.parametrize("name", BAD, ids=repr)
def test_rename_refuses_a_name_that_is_not_a_string(world, name):
    for path in (f"/speakers/{world['speaker']}", f"/api/v1/speakers/{world['speaker']}"):
        resp = send(world, "PUT", path, json_body={"name": name})
        assert resp.status_code == 400, (path, resp.status_code)
        assert code_of(resp) in ("speaker_name_empty", "invalid_parameter"), path
    with app.app_context():
        assert db.session.get(Speaker, world["speaker"]).name == f"Dana {world['suffix']}"


def test_a_padded_name_is_stripped(world):
    resp = send(world, "POST", "/api/v1/speakers", json_body={"name": "  Lee Park  "})
    assert resp.status_code == 201
    assert resp.get_json()["name"] == "Lee Park"
    resp = send(world, "PUT", f"/speakers/{world['speaker']}", json_body={"name": " Robin "})
    assert resp.status_code == 200 and resp.get_json()["speaker"]["name"] == "Robin"
