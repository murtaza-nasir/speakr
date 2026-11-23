"""An id past the range of an Integer column is a client error, never a
500 (#664, #676, #677).

Integer columns are INT4 on PostgreSQL, so no row has an id above
2**31 - 1. A larger id in a URL matches no route (404 at routing); a
larger id in a body is refused with 400 before any lookup.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app
from src.utils.request_body import DB_INT_MAX, is_db_id, is_int_list
from tests.request_body_support import code_of, headers, make_world, send

HUGE = 2 ** 63 + 1


@pytest.fixture
def world():
    with make_world() as ids:
        yield ids


def test_is_db_id_holds_the_range_of_an_integer_column():
    assert is_db_id(1) and is_db_id(DB_INT_MAX) and is_db_id(0)
    assert not is_db_id(DB_INT_MAX + 1) and not is_db_id(HUGE) and not is_db_id(-HUGE)
    assert not is_db_id(True) and not is_db_id("1") and not is_db_id(1.0) and not is_db_id([1])
    assert is_int_list([1, DB_INT_MAX]) and not is_int_list([1, DB_INT_MAX + 1])


def test_the_int_url_converter_stops_at_the_column_range():
    adapter = app.url_map.bind("localhost")
    endpoint, args = adapter.match(f"/api/v1/recordings/{DB_INT_MAX}")
    assert args == {"recording_id": DB_INT_MAX}
    for path in (f"/api/v1/recordings/{DB_INT_MAX + 1}", f"/api/v1/recordings/{HUGE}"):
        try:
            endpoint, _ = adapter.match(path)
        except Exception:
            continue
        assert endpoint != "api_v1.get_recording", path


def _int_rules():
    for rule in app.url_map.iter_rules():
        if "<int:" in rule.rule:
            yield rule


def test_every_int_route_answers_a_huge_id_without_a_server_error(world):
    """Each <int:...> rule, with a huge value for every id, by each of its
    methods: no 500, and the rule itself does not match."""
    rules = list(_int_rules())
    assert len(rules) > 100
    with app.test_client() as c:
        for rule in rules:
            path = re.sub(r"<int:[a-z_]+>", str(HUGE), rule.rule)
            path = re.sub(r"<(?:[a-z]+:)?[a-z_]+>", "x", path)
            for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
                resp = c.open(path, method=method, headers=headers(world), json={})
                assert resp.status_code < 500, (method, path, resp.status_code)
                try:
                    matched = app.url_map.bind("localhost").match(path, method=method)[0]
                except Exception:
                    continue  # no rule takes it: 404 or 405
                # Another rule took it (a string converter); never this one
                assert matched != rule.endpoint, (method, path)


def test_a_huge_recording_id_in_the_path_is_404(world):
    for path in (f"/api/v1/recordings/{HUGE}", f"/api/recordings/{HUGE}/tags/1",
                 f"/api/v1/recordings/{DB_INT_MAX + 1}"):
        resp = send(world, "GET", path, content_type=None)
        assert resp.status_code in (404, 405), (path, resp.status_code)


@pytest.mark.parametrize("folder_id", [[1], "1", HUGE, DB_INT_MAX + 1, True, {"id": 1}])
def test_recording_patch_refuses_a_folder_id_that_is_not_an_id(world, folder_id):
    resp = send(world, "PATCH", f"/api/v1/recordings/{world['rec']}", json_body={"folder_id": folder_id})
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert code_of(resp) == "invalid_folder_id"
    assert "[1]" not in resp.get_json()["error"]


@pytest.mark.parametrize("folder_id", [[1], HUGE])
def test_batch_patch_refuses_a_folder_id_that_is_not_an_id(world, folder_id):
    resp = send(world, "PATCH", "/api/v1/recordings/batch",
                json_body={"recording_ids": [world["rec"]], "updates": {"folder_id": folder_id}})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_folder_id"


def test_a_huge_unknown_folder_id_in_range_is_still_404(world):
    resp = send(world, "PATCH", f"/api/v1/recordings/{world['rec']}", json_body={"folder_id": DB_INT_MAX})
    assert resp.status_code == 404


def test_a_huge_tag_id_in_a_body_is_400(world):
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/tags", json_body={"tag_id": HUGE})
    assert resp.status_code == 400
    resp = send(world, "POST", f"/api/v1/recordings/{world['rec']}/tags", json_body={"tag_ids": [HUGE]})
    assert resp.status_code == 400
    resp = send(world, "PUT", f"/api/v1/recordings/{world['rec']}/tags", json_body={"tag_ids": [HUGE]})
    assert resp.status_code == 400
