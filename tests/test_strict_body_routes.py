"""Routes that read request.json without a check answer 400, never 500 (#687 follow-up).

A JSON array or string where an object belongs, and a field of the wrong
type (a name that is a number, a flag that is "maybe", an id past the
column range), are refused before any change.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards; the template and users made by single tests too.
"""

import os
import secrets
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, GroupMembership, NamingTemplate, Share, SystemSetting, User
from tests.request_body_support import code_of, make_world, send

HUGE = 2 ** 63 + 1


@pytest.fixture
def world():
    with make_world() as ids, \
            patch("src.api.shares.ENABLE_PUBLIC_SHARING", True, create=True), \
            patch("src.api.shares.link_is_https", return_value=True), \
            patch("src.api.groups.ENABLE_INTERNAL_SHARING", True, create=True):
        try:
            yield ids
        finally:
            with app.app_context():
                Share.query.filter_by(user_id=ids["owner"]).delete()
                NamingTemplate.query.filter_by(user_id=ids["owner"]).delete()
                APIToken.query.filter(APIToken.user_id == ids["owner"], APIToken.name != "owner").delete(
                    synchronize_session=False)
                User.query.filter(User.username.like(f"sb{ids['suffix']}%")).delete(synchronize_session=False)
                SystemSetting.query.filter_by(key=f"sb_{ids['suffix']}").delete()
                db.session.commit()


def _template(world):
    with app.app_context():
        t = NamingTemplate(user_id=world["owner"], name="t", template="{{ai_title}}")
        db.session.add(t)
        db.session.commit()
        return t.id


def _routes(world, template_id):
    rec, g, owner = world["rec"], world["group"], world["owner"]
    return [
        ("owner", "POST", "/api/tokens"),
        ("owner", "POST", f"/api/naming-templates/{template_id}/test"),
        ("owner", "PUT", "/api/naming-templates/default"),
        ("admin", "PUT", f"/api/admin/groups/{g}/members/{owner}"),
        ("owner", "POST", "/api/user/auto-speaker-labelling"),
        ("owner", "POST", "/api/user/auto-summarization"),
        ("owner", "POST", f"/api/recording/{rec}/share"),
        ("admin", "POST", "/admin/users"),
        ("admin", "PUT", f"/admin/users/{owner}"),
        ("admin", "POST", "/admin/settings"),
        ("admin", "POST", "/admin/auto-process/config"),
        ("admin", "POST", "/admin/transcription/visible-models"),
        ("admin", "POST", "/admin/inquire/process-recordings"),
    ]


@pytest.mark.parametrize("body", [[1], "x", 5], ids=repr)
def test_a_body_that_is_not_an_object_is_400(world, body):
    template_id = _template(world)
    failures = []
    for who, method, path in _routes(world, template_id):
        resp = send(world, method, path, json_body=body, who=who)
        if resp.status_code != 400 or code_of(resp) != "body_must_be_object":
            failures.append((method, path, resp.status_code))
    assert failures == []


def test_a_public_share_update_refuses_a_body_that_is_not_an_object(world):
    with app.app_context():
        share = Share(recording_id=world["rec"], user_id=world["owner"], share_summary=True, share_notes=True)
        db.session.add(share)
        db.session.commit()
        sid = share.id
    for body in ([1], {"share_summary": "maybe"}, {"share_notes": None}):
        resp = send(world, "PUT", f"/api/share/{sid}", json_body=body)
        assert resp.status_code == 400, (body, resp.status_code)
    resp = send(world, "PUT", f"/api/share/{sid}", json_body={"share_summary": "false"})
    assert resp.status_code == 200 and resp.get_json()["share"]["share_summary"] is False


@pytest.mark.parametrize("body,code", [
    ({"name": 5}, "field_must_be_string"),
    ({"name": "x" * 101}, "token_name_too_long"),
    ({"expires_in_days": 10 ** 12}, "invalid_expires_in_days"),
    ({"expires_in_days": True}, "invalid_expires_in_days"),
], ids=repr)
def test_token_creation_refuses_fields_of_the_wrong_type(world, body, code):
    resp = send(world, "POST", "/api/tokens", json_body=body)
    assert resp.status_code == 400 and code_of(resp) == code


def test_naming_template_routes_refuse_fields_of_the_wrong_type(world):
    template_id = _template(world)
    for body in ({"filename": 5}, {"date": 5}, {"ai_title": ["x"]}):
        resp = send(world, "POST", f"/api/naming-templates/{template_id}/test", json_body=body)
        assert resp.status_code == 400 and code_of(resp) == "field_must_be_string", body
    for value in (HUGE, "1", [1], True):
        resp = send(world, "PUT", "/api/naming-templates/default", json_body={"template_id": value})
        assert resp.status_code == 400 and code_of(resp) == "invalid_integer_value", value
    resp = send(world, "PUT", "/api/naming-templates/default", json_body={"template_id": None})
    assert resp.status_code == 200


def test_user_setting_flags_read_false(world):
    for path, column in (("/api/user/auto-speaker-labelling", "auto_speaker_labelling"),
                         ("/api/user/auto-summarization", "auto_summarization")):
        resp = send(world, "POST", path, json_body={"enabled": "false"})
        assert resp.status_code == 200
        with app.app_context():
            assert getattr(db.session.get(User, world["owner"]), column) is False
        resp = send(world, "POST", path, json_body={"enabled": "maybe"})
        assert resp.status_code == 400 and code_of(resp) == "invalid_boolean_flag"


def test_public_share_creation_refuses_a_flag_that_is_not_one(world):
    resp = send(world, "POST", f"/api/recording/{world['rec']}/share", json_body={"share_summary": "maybe"})
    assert resp.status_code == 400 and code_of(resp) == "invalid_boolean_flag"


@pytest.mark.parametrize("body,code", [
    ({"username": ["a", "b"], "email": "x@local.test", "password": "pw"}, "field_must_be_string"),
    ({"username": "okname", "email": 5, "password": "pw"}, "field_must_be_string"),
    ({"username": "okname", "email": "x@local.test", "password": 5}, "field_must_be_string"),
    ({"username": "okname", "email": "x" * 121, "password": "pw"}, "field_too_long"),
    ({"username": "okname", "email": "x@local.test", "password": "pw", "is_admin": "maybe"}, "invalid_boolean_flag"),
    ({"username": "okname", "email": "x@local.test", "password": "pw", "storage_quota_mb": HUGE},
     "invalid_storage_quota"),
], ids=repr)
def test_admin_user_creation_refuses_fields_of_the_wrong_type(world, body, code):
    resp = send(world, "POST", "/admin/users", json_body=body, who="admin")
    assert resp.status_code == 400 and code_of(resp) == code, resp.get_data(as_text=True)[:200]


@pytest.mark.parametrize("body,code", [
    ({"username": 5}, "field_must_be_string"),
    ({"email": ["x"]}, "field_must_be_string"),
    ({"can_share_publicly": "maybe"}, "invalid_boolean_flag"),
    ({"is_admin": []}, "invalid_boolean_flag"),
    ({"create_groups_granted": 2}, "invalid_boolean_flag"),
    ({"monthly_token_budget": HUGE}, "invalid_token_budget"),
], ids=repr)
def test_admin_user_update_refuses_fields_of_the_wrong_type_before_any_change(world, body, code):
    with app.app_context():
        before = db.session.get(User, world["owner"]).username
    resp = send(world, "PUT", f"/admin/users/{world['owner']}", json_body={"username": "sbchanged", **body}
                if "username" not in body else body, who="admin")
    assert resp.status_code == 400 and code_of(resp) == code, resp.get_data(as_text=True)[:200]
    with app.app_context():
        assert db.session.get(User, world["owner"]).username == before


def test_admin_user_update_reads_the_string_false(world):
    resp = send(world, "PUT", f"/admin/users/{world['owner']}", json_body={"can_share_publicly": "false"},
                who="admin")
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(User, world["owner"]).can_share_publicly is False


def test_admin_settings_and_tools_refuse_fields_of_the_wrong_type(world):
    for body, code in (({"key": ["x"], "value": "1"}, "field_must_be_string"),
                       ({"key": f"sb_{world['suffix']}", "value": "1", "setting_type": ["x"]},
                        "field_must_be_string"),
                       ({"key": f"sb_{world['suffix']}", "value": "1", "description": 5},
                        "field_must_be_string")):
        resp = send(world, "POST", "/admin/settings", json_body=body, who="admin")
        assert resp.status_code == 400 and code_of(resp) == code, body
    for body in ({"batch_size": "abc"}, {"batch_size": 0}, {"max_recordings": "x"}, {"force": "maybe"}):
        resp = send(world, "POST", "/admin/inquire/process-recordings", json_body=body, who="admin")
        assert resp.status_code == 400, (body, resp.status_code)


def test_group_member_role_refuses_a_body_that_is_not_an_object(world):
    with app.app_context():
        if not GroupMembership.query.filter_by(group_id=world["group"], user_id=world["owner"]).first():
            pytest.skip("owner is not a member")
    resp = send(world, "PUT", f"/api/admin/groups/{world['group']}/members/{world['owner']}",
                json_body={"role": ["admin"]}, who="admin")
    assert resp.status_code == 400 and code_of(resp) == "invalid_group_role"
