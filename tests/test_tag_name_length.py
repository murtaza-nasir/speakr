"""A tag name is a string of at most 50 characters, stripped (#682).

Tag.name is String(50): PostgreSQL refuses a longer value at commit, so
every route that names a tag (web create, web update, group tags, API v1
create and update) checks it first and answers 400 tag_name_too_long.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys

from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Tag
from tests.request_body_support import code_of, make_world, send

LONG = "x" * 51


@pytest.fixture
def world():
    with make_world() as ids, \
            patch("src.api.tags.ENABLE_INTERNAL_SHARING", True, create=True), \
            patch("src.app.ENABLE_INTERNAL_SHARING", True, create=True):
        yield ids


def _create_routes(world):
    return ["/api/tags", "/api/v1/tags", f"/api/groups/{world['group']}/tags"]


def test_create_refuses_a_name_over_50_characters(world):
    for path in _create_routes(world):
        resp = send(world, "POST", path, json_body={"name": LONG})
        assert resp.status_code == 400, (path, resp.status_code)
        assert code_of(resp) == "tag_name_too_long", path
        assert "50" in resp.get_json()["error"]
    with app.app_context():
        assert Tag.query.filter_by(name=LONG).count() == 0


@pytest.mark.parametrize("name", [None, 5, ["a"], "", "   "], ids=repr)
def test_create_refuses_a_name_that_is_not_a_string(world, name):
    for path in _create_routes(world):
        resp = send(world, "POST", path, json_body={"name": name})
        assert resp.status_code == 400 and code_of(resp) == "tag_name_required", path


def test_a_name_of_50_characters_padded_with_spaces_is_kept_stripped(world):
    for i, path in enumerate(_create_routes(world)):
        name = f"{i}" + "y" * 49
        resp = send(world, "POST", path, json_body={"name": f"  {name}  "})
        assert resp.status_code in (200, 201), (path, resp.get_data(as_text=True)[:200])
        with app.app_context():
            assert Tag.query.filter_by(name=name).count() == 1, path


@pytest.mark.parametrize("name,code", [(LONG, "tag_name_too_long"), (None, "tag_name_required"),
                                       (7, "tag_name_required"), ("  ", "tag_name_required")])
def test_update_refuses_a_bad_name_and_keeps_the_old_one(world, name, code):
    for path in (f"/api/tags/{world['a']}", f"/api/v1/tags/{world['a']}"):
        resp = send(world, "PUT", path, json_body={"name": name})
        assert resp.status_code == 400 and code_of(resp) == code, (path, resp.status_code)
    with app.app_context():
        assert db.session.get(Tag, world["a"]).name == f"a-{world['suffix']}"


def test_update_strips_the_name(world):
    resp = send(world, "PUT", f"/api/v1/tags/{world['a']}", json_body={"name": "  Renamed  "})
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Tag, world["a"]).name == "Renamed"
