"""An id in the query string past the range of an Integer column is a
client error, never a 500 (#664 follow-up).

The list and search filters (tag_id, folder_id, owner_id, user_id,
recording_ids, before_id, after_id, template_id) answer 400 for an
out-of-range id; page numbers too large to page to answer 400 as well.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.request_body import DB_INT_MAX
from tests.request_body_support import make_world, send

HUGE = 2 ** 63 + 1


@pytest.fixture
def world():
    with make_world() as ids:
        yield ids


def _paths(world):
    rec = world["rec"]
    return [
        ("owner", f"/api/v1/recordings?tag_id={HUGE}"),
        ("owner", f"/api/v1/recordings?folder_id={HUGE}"),
        ("owner", f"/api/v1/recordings?owner_id={HUGE}"),
        ("owner", f"/api/v1/recordings?page={HUGE}"),
        ("owner", f"/api/v1/recordings?tag_id={DB_INT_MAX + 1}"),
        ("owner", f"/api/v1/search?q=hello&tag_id={HUGE}"),
        ("owner", f"/api/v1/search?q=hello&folder_id={HUGE}"),
        ("owner", f"/api/v1/search?q=hello&recording_ids=1,{HUGE}"),
        ("owner", f"/api/v1/search?q=hello&page={HUGE}"),
        ("owner", f"/api/recordings?folder={HUGE}"),
        ("owner", f"/api/recordings?page={HUGE}"),
        ("owner", f"/api/v1/recordings/{rec}/transcript?format=txt&template_id={HUGE}"),
        ("owner", f"/recording/{rec}/download/transcript?template_id={HUGE}"),
        ("admin", f"/admin/audit-log?user={HUGE}"),
        ("admin", f"/admin/audit-log?recording_id={HUGE}"),
        ("admin", f"/admin/audit-log?before_id={HUGE}"),
        ("admin", f"/admin/audit-log?after_id={HUGE}"),
        ("admin", f"/admin/token-stats/daily?user_id={HUGE}"),
        ("admin", f"/admin/transcription-stats/daily?user_id={HUGE}"),
        ("admin", f"/admin/token-stats/daily?days={HUGE}"),
        ("admin", f"/admin/transcription-stats/daily?days={HUGE}"),
        ("admin", f"/admin/token-stats/monthly?months={HUGE}"),
        ("admin", f"/admin/transcription-stats/monthly?months={HUGE}"),
        ("owner", f"/api/v1/recordings/changes?limit={HUGE}"),
    ]


def test_an_out_of_range_query_id_is_400(world):
    failures = []
    for who, path in _paths(world):
        resp = send(world, "GET", path, content_type=None, who=who)
        if resp.status_code != 400:
            failures.append((path, resp.status_code))
    assert failures == []


def test_a_huge_count_is_refused_before_it_is_looped_over(world):
    """months=2**63 looped once per month (the process ran out of memory),
    days=2**63 overflowed the start date: both are bounded."""
    for path in ("/admin/transcription-stats/monthly?months=1201", "/admin/token-stats/daily?days=3651",
                 "/admin/token-stats/monthly?months=0"):
        resp = send(world, "GET", path, content_type=None, who="admin")
        assert resp.status_code == 400 and resp.get_json()["code"] == "field_out_of_range", path
    assert send(world, "GET", "/admin/transcription-stats/monthly?months=12", content_type=None,
                who="admin").status_code == 200


def test_no_query_value_is_a_server_error(world):
    errors = []
    for who, path in _paths(world):
        resp = send(world, "GET", path, content_type=None, who=who)
        if resp.status_code >= 500:
            errors.append((path, resp.status_code))
    assert errors == []


def test_a_huge_page_of_the_admin_user_list_is_an_empty_page(world):
    """That route clamps its paging values instead of refusing them."""
    resp = send(world, "GET", f"/admin/users?page={HUGE}", content_type=None, who="admin")
    assert resp.status_code == 200


def test_an_id_in_range_still_filters(world):
    resp = send(world, "GET", f"/api/v1/recordings?tag_id={DB_INT_MAX}", content_type=None)
    assert resp.status_code == 200
    assert resp.get_json()["recordings"] == []
    resp = send(world, "GET", f"/api/v1/recordings?tag_id={world['a']}", content_type=None)
    assert resp.status_code == 200
    assert [r["id"] for r in resp.get_json()["recordings"]] == [world["rec"]]
