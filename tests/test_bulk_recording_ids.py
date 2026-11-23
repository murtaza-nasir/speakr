"""recording_ids that is not a list of ids is a 400, never a 500 (#666).

Every bulk and batch route checks the whole list before its first change:
a string, a number, an object, a list holding anything but whole numbers
or an id past the column range answers 400 invalid_recording_ids_list.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.request_body_support import code_of, make_world, send

HUGE = 2 ** 63 + 1

BAD_LISTS = ["abc", 5, {"id": 1}, ["1"], [1.5], [True], [None], [[1]], [HUGE]]


@pytest.fixture
def world():
    with make_world() as ids:
        with patch("src.api.folders.SystemSetting.get_setting", return_value=True):
            yield ids


def _routes(world):
    rec, tag = world["rec"], world["a"]
    return [
        ("POST", "/api/recordings/bulk-reprocess", {"type": "summary"}),
        ("POST", "/api/recordings/bulk-tags", {"tag_id": tag, "action": "add"}),
        ("POST", "/api/recordings/bulk-toggle", {"field": "inbox", "value": True}),
        ("DELETE", "/api/recordings/bulk", {}),
        ("POST", "/api/recordings/bulk/folder", {"folder_id": None}),
        ("PATCH", "/api/v1/recordings/batch", {"updates": {"is_inbox": True}}),
        ("DELETE", "/api/v1/recordings/batch", {}),
        ("POST", "/api/v1/recordings/batch/transcribe", {}),
    ]


@pytest.mark.parametrize("bad", BAD_LISTS, ids=repr)
def test_every_bulk_route_refuses_recording_ids_that_is_not_a_list_of_ids(world, bad):
    for method, path, rest in _routes(world):
        resp = send(world, method, path, json_body={"recording_ids": bad, **rest})
        assert resp.status_code == 400, (method, path, resp.status_code, resp.get_data(as_text=True)[:200])
        assert code_of(resp) == "invalid_recording_ids_list", (method, path)


def test_a_valid_list_still_works(world):
    resp = send(world, "POST", "/api/recordings/bulk-toggle",
                json_body={"recording_ids": [world["rec"]], "field": "highlight", "value": True})
    assert resp.status_code == 200
    assert resp.get_json()["affected_ids"] == [world["rec"]]
    resp = send(world, "PATCH", "/api/v1/recordings/batch",
                json_body={"recording_ids": [world["rec"]], "updates": {"is_inbox": False}})
    assert resp.status_code == 200 and resp.get_json()["updated"] == 1


@pytest.mark.parametrize("body", [[1], "x", 3])
def test_batch_routes_refuse_a_body_that_is_not_an_object(world, body):
    for method, path in (("PATCH", "/api/v1/recordings/batch"), ("DELETE", "/api/v1/recordings/batch"),
                         ("POST", "/api/v1/recordings/batch/transcribe"), ("POST", "/api/recordings/bulk/folder")):
        resp = send(world, method, path, json_body=body)
        assert resp.status_code == 400 and code_of(resp) == "body_must_be_object", (method, path)


@pytest.mark.parametrize("updates", [[1], "x", None])
def test_batch_patch_refuses_updates_that_is_not_an_object(world, updates):
    resp = send(world, "PATCH", "/api/v1/recordings/batch",
                json_body={"recording_ids": [world["rec"]], "updates": updates})
    assert resp.status_code == 400 and code_of(resp) == "invalid_updates"


@pytest.mark.parametrize("key", ["add_tag_ids", "remove_tag_ids"])
@pytest.mark.parametrize("bad", ["1", [HUGE], 3])
def test_batch_patch_refuses_tag_ids_that_are_not_ids(world, key, bad):
    resp = send(world, "PATCH", "/api/v1/recordings/batch",
                json_body={"recording_ids": [world["rec"]], "updates": {key: bad}})
    assert resp.status_code == 400 and code_of(resp) == "invalid_tag_ids"


@pytest.mark.parametrize("tag_id", [HUGE, "1", [1]])
def test_bulk_tags_refuses_a_tag_id_that_is_not_an_id(world, tag_id):
    resp = send(world, "POST", "/api/recordings/bulk-tags",
                json_body={"recording_ids": [world["rec"]], "tag_id": tag_id})
    assert resp.status_code == 400 and code_of(resp) == "invalid_integer_value"


@pytest.mark.parametrize("folder_id", [[1], HUGE, "1"])
def test_folder_assignment_refuses_a_folder_id_that_is_not_an_id(world, folder_id):
    resp = send(world, "PUT", f"/api/recordings/{world['rec']}/folder", json_body={"folder_id": folder_id})
    assert resp.status_code == 400 and code_of(resp) == "invalid_folder_id"
    resp = send(world, "POST", "/api/recordings/bulk/folder",
                json_body={"recording_ids": [world["rec"]], "folder_id": folder_id})
    assert resp.status_code == 400 and code_of(resp) == "invalid_folder_id"
