"""Tag writes refuse a body that is not an object (#656), and the recording
tag routes refuse tag_ids that are not a list of integers (#657).

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import RecordingTag, Tag
from tests.request_body_support import code_of, make_world, send


@pytest.fixture
def world():
    with make_world() as ids, patch("src.api.tags.ENABLE_INTERNAL_SHARING", True, create=True):
        yield ids


def _tags(world):
    with app.app_context():
        return [rt.tag_id for rt in RecordingTag.query.filter_by(recording_id=world["rec"]).order_by(RecordingTag.order)]


def _orders(world):
    with app.app_context():
        return {rt.tag_id: rt.order for rt in RecordingTag.query.filter_by(recording_id=world["rec"])}


def _tag_name(world, tid):
    with app.app_context():
        return db.session.get(Tag, tid).name


def _attach_b(world):
    with app.app_context():
        db.session.add(RecordingTag(recording_id=world["rec"], tag_id=world["b"], order=2))
        db.session.commit()


# ------------------------------------------------------------------ #656

@pytest.mark.parametrize("route", ["/api/v1/tags", "/api/tags", "/api/groups/{group}/tags"])
@pytest.mark.parametrize("payload", [42, True, "x", [1], []])
def test_tag_create_rejects_non_object_json(world, route, payload):
    resp = send(world, "POST", route.format(group=world["group"]), json_body=payload)
    assert resp.status_code == 400
    assert code_of(resp) == "body_must_be_object"


@pytest.mark.parametrize("route", ["/api/v1/tags", "/api/tags"])
@pytest.mark.parametrize("payload", [42, "x", [1]])
def test_tag_update_rejects_non_object_json(world, route, payload):
    before = _tag_name(world, world["a"])
    resp = send(world, "PUT", f"{route}/{world['a']}", json_body=payload)
    assert resp.status_code == 400
    assert code_of(resp) == "body_must_be_object"
    assert _tag_name(world, world["a"]) == before


def test_tag_create_and_update_accept_json_objects(world):
    s = world["suffix"]
    resp = send(world, "POST", "/api/v1/tags", json_body={"name": f"new-{s}"})
    assert resp.status_code == 201
    resp = send(world, "POST", "/api/tags", json_body={"name": f"web-{s}"})
    assert resp.status_code == 201
    resp = send(world, "POST", f"/api/groups/{world['group']}/tags", json_body={"name": f"grp-{s}"})
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:300]
    for route in ("/api/v1/tags", "/api/tags", f"/api/groups/{world['group']}/tags"):
        resp = send(world, "POST", route, json_body={})
        assert resp.status_code == 400 and code_of(resp) == "tag_name_required"
    resp = send(world, "POST", f"/api/groups/{world['group']}/tags", json_body={"name": 5})
    assert resp.status_code == 400 and code_of(resp) == "tag_name_required"
    resp = send(world, "PUT", f"/api/v1/tags/{world['a']}", json_body={"name": f"renamed-{s}"})
    assert resp.status_code == 200
    assert _tag_name(world, world["a"]) == f"renamed-{s}"
    resp = send(world, "PUT", f"/api/tags/{world['a']}", json_body={"name": f"again-{s}"})
    assert resp.status_code == 200
    assert _tag_name(world, world["a"]) == f"again-{s}"


# ------------------------------------------------------------------ #657

@pytest.mark.parametrize("value", [5, "ab", {"a": 1}, True, ["ab"], [True], [{}], [[1]], "3", [1.5]])
def test_post_rejects_tag_ids_that_are_not_a_list_of_integers(world, value):
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/tags", json_body={"tag_ids": value})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_parameter"
    assert _tags(world) == [world["a"]]


@pytest.mark.parametrize("payload,code", [
    ({"tag_id": "ab"}, "invalid_parameter"),
    ({"tag_id": True}, "invalid_parameter"),
    ({"tag_id": "5"}, "invalid_parameter"),
    ({"tag_id": 2.5}, "invalid_parameter"),
    ({"tag_ids": []}, "tag_ids_required"),
    ({"tag_ids": None}, "tag_ids_required"),
    ({}, "tag_ids_required"),
])
def test_post_legacy_and_empty_bodies_keep_their_contract(world, payload, code):
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/tags", json_body=payload)
    assert resp.status_code == 400
    assert code_of(resp) == code
    assert _tags(world) == [world["a"]]


@pytest.mark.parametrize("field", ["tag_ids", "tag_id"])
def test_post_accepts_a_list_and_the_legacy_single_id(world, field):
    payload = {"tag_ids": [world["b"]]} if field == "tag_ids" else {"tag_id": world["b"]}
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/tags", json_body=payload)
    assert resp.status_code == 200
    assert world["b"] in [t["id"] for t in resp.get_json()["added_tags"]]
    assert _tags(world) == [world["a"], world["b"]]
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/tags", json_body={"tag_ids": [999999999]})
    assert resp.status_code == 200 and resp.get_json()["errors"]


@pytest.mark.parametrize("value", ["abc", None, {}, [], True, 1.5, "3"])
def test_reorder_rejects_non_integer_tag_ids(world, value):
    _attach_b(world)
    before = _orders(world)
    resp = send(world, "PUT", f"/api/recordings/{world['rec']}/tags/reorder", json_body={"tag_ids": [value]})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_tag_ids"
    assert _orders(world) == before
    # A mixed list is refused before the first change
    resp = send(world, "PUT", f"/api/recordings/{world['rec']}/tags/reorder",
                json_body={"tag_ids": [world["b"], value]})
    assert resp.status_code == 400 and code_of(resp) == "invalid_tag_ids"
    assert _orders(world) == {world["a"]: 1, world["b"]: 2}


@pytest.mark.parametrize("value", [5, "ab", {"a": 1}, True])
def test_reorder_rejects_tag_ids_that_are_not_a_list(world, value):
    resp = send(world, "PUT", f"/api/recordings/{world['rec']}/tags/reorder", json_body={"tag_ids": value})
    assert resp.status_code == 400 and code_of(resp) == "invalid_tag_ids"


def test_reorder_accepts_integer_ids_and_deduplicates(world):
    _attach_b(world)
    resp = send(world, "PUT", f"/api/recordings/{world['rec']}/tags/reorder",
                json_body={"tag_ids": [world["b"], world["a"], world["b"]]})
    assert resp.status_code == 200
    assert _orders(world) == {world["b"]: 1, world["a"]: 2}
    resp = send(world, "PUT", f"/api/recordings/{world['rec']}/tags/reorder", json_body={"tag_ids": []})
    assert resp.status_code == 200
    # An id that is not attached is ignored
    resp = send(world, "PUT", f"/api/recordings/{world['rec']}/tags/reorder",
                json_body={"tag_ids": [999999999, world["a"]]})
    assert resp.status_code == 200
