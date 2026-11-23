"""A body or a field of the wrong type is a 400, never a 500 (#687).

The routes that read their body with get_json(silent=True) or {} took a
JSON array, a string or a number for an object, and called .strip() or
.get() on whatever a field held. Each now parses its body with
json_object_body and checks the type of every field it uses; a route
whose body is optional still accepts no body (#621).

SHARED-DB: the users, recording, tags, speaker, group, tokens, webhooks
and sessions made here are removed afterwards.
"""

import os
import sys
import uuid
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import RecordingSession, Webhook
from tests.request_body_support import code_of, make_world, send

HUGE = 2 ** 63 + 1


@pytest.fixture
def world():
    with make_world() as ids, \
            patch("src.api.folders.SystemSetting.get_setting", return_value=True), \
            patch("src.api.groups.ENABLE_INTERNAL_SHARING", True, create=True), \
            patch("src.api.tags.ENABLE_INTERNAL_SHARING", True, create=True), \
            patch("src.app.ENABLE_INTERNAL_SHARING", True, create=True):
        try:
            yield ids
        finally:
            with app.app_context():
                users = [ids["owner"], ids["admin"]]
                RecordingSession.query.filter(RecordingSession.user_id.in_(users)).delete(synchronize_session=False)
                Webhook.query.filter(Webhook.user_id.in_(users)).delete(synchronize_session=False)
                db.session.commit()


# ------------------------------------------------------------ upload sessions

@pytest.mark.parametrize("body,code", [
    ({"mime_type": 5}, "field_must_be_string"),
    ({"mime_type": ["audio/webm"]}, "field_must_be_string"),
    ({"filename": 7, "total_bytes": 10}, "field_must_be_string"),
    ({"filename": {"a": 1}}, "field_must_be_string"),
    ({"mime_type": "audio/" + "x" * 100}, "field_too_long"),
    ({"filename": "a.wav", "total_bytes": True}, "invalid_total_bytes"),
    ({"filename": "a.wav", "total_bytes": 2.5}, "invalid_total_bytes"),
    ({"filename": "a.wav", "total_bytes": 1e300}, "invalid_total_bytes"),
    ({"filename": "a.wav", "total_bytes": 2 ** 70}, "invalid_total_bytes"),
    ({"filename": "a.wav", "total_bytes": "many"}, "invalid_total_bytes"),
    ({"filename": "a.wav"}, "total_bytes_required"),
    ([1], "body_must_be_object"),
    ("audio/webm", "body_must_be_object"),
], ids=repr)
def test_upload_session_refuses_fields_of_the_wrong_type(world, body, code):
    resp = send(world, "POST", "/upload/session", json_body=body)
    assert resp.status_code == 400, resp.get_data(as_text=True)[:200]
    assert code_of(resp) == code


def test_upload_session_still_opens_without_a_body(world):
    resp = send(world, "POST", "/upload/session", content_type=None)
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:200]
    assert resp.get_json()["session_id"]
    resp = send(world, "POST", "/upload/session", json_body={"mime_type": None})
    assert resp.status_code in (200, 201)


def _open_session(world):
    with app.app_context():
        sid = str(uuid.uuid4())
        db.session.add(RecordingSession(id=sid, user_id=world["owner"], mime_type="audio/webm",
                                        kind="recorder", status="recording", chunk_count=1))
        db.session.commit()
        return sid


@pytest.mark.parametrize("body,code", [
    ({"title": 5}, "invalid_parameter"),
    ({"notes": ["x"]}, "invalid_parameter"),
    ({"folder_id": HUGE}, "invalid_folder_id"),
    ({"tags": 5}, "invalid_tag_ids"),
    ([1], "invalid_finalize_body"),
], ids=repr)
def test_finalize_refuses_fields_of_the_wrong_type_before_any_change(world, body, code):
    sid = _open_session(world)
    resp = send(world, "POST", f"/upload/session/{sid}/finalize", json_body=body)
    assert resp.status_code == 400, resp.get_data(as_text=True)[:200]
    assert code_of(resp) == code
    with app.app_context():
        assert db.session.get(RecordingSession, sid).status == "recording"


# ------------------------------------------------------------ webhooks

def _webhook(world):
    with app.app_context():
        wh = Webhook(user_id=world["owner"], name="hook", url="https://hooks.example.test/x",
                     allow_http=False, enabled=True, include_shared=False, secret="s" * 32)
        wh.event_list = ["recording.created"]
        db.session.add(wh)
        db.session.commit()
        return wh.id


@pytest.mark.parametrize("body,code", [
    ({"name": 5, "url": "https://hooks.example.test", "events": ["recording.created"]}, "field_must_be_string"),
    ({"name": "a", "url": ["https://x"], "events": ["recording.created"]}, "field_must_be_string"),
    ({"name": "a", "url": "https://hooks.example.test", "events": ["recording.created"],
      "allow_http": "maybe"}, "invalid_boolean_flag"),
    ([1], "body_must_be_object"),
], ids=repr)
def test_webhook_create_refuses_fields_of_the_wrong_type(world, body, code):
    resp = send(world, "POST", "/api/v1/webhooks", json_body=body)
    assert resp.status_code == 400 and code_of(resp) == code


def test_webhook_update_reads_flags_and_refuses_wrong_types(world):
    wid = _webhook(world)
    resp = send(world, "PATCH", f"/api/v1/webhooks/{wid}", json_body={"enabled": "false", "include_shared": "true"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
    with app.app_context():
        wh = db.session.get(Webhook, wid)
        assert wh.enabled is False and wh.include_shared is True
    for body, code in (({"name": 5}, "field_must_be_string"), ({"enabled": 3}, "invalid_boolean_flag"),
                       ("x", "body_must_be_object")):
        resp = send(world, "PATCH", f"/api/v1/webhooks/{wid}", json_body=body)
        assert resp.status_code == 400 and code_of(resp) == code, body
    # No body changes nothing and is not an error
    assert send(world, "PATCH", f"/api/v1/webhooks/{wid}", content_type=None).status_code == 200


# ------------------------------------------------------------ groups

@pytest.mark.parametrize("body,code", [
    ({"name": 5}, "field_must_be_string"),
    ({"name": "ok", "description": 5}, "field_must_be_string"),
    ({"name": "x" * 101}, "field_too_long"),
    ([1], "body_must_be_object"),
], ids=repr)
def test_group_create_refuses_fields_of_the_wrong_type(world, body, code):
    resp = send(world, "POST", "/api/admin/groups", json_body=body, who="admin")
    assert resp.status_code == 400 and code_of(resp) == code


def test_group_update_and_members_refuse_wrong_types(world):
    g = world["group"]
    for body, code in (({"name": ["x"]}, "field_must_be_string"),
                       ({"members_create_groups_granted": "maybe"}, "invalid_boolean_flag"),
                       ([1], "body_must_be_object")):
        resp = send(world, "PUT", f"/api/admin/groups/{g}", json_body=body, who="admin")
        assert resp.status_code == 400 and code_of(resp) == code, body
    resp = send(world, "POST", f"/api/admin/groups/{g}/members", json_body={"user_id": HUGE}, who="admin")
    assert resp.status_code == 400 and code_of(resp) == "invalid_integer_value"
    resp = send(world, "POST", f"/api/admin/groups/{g}/members", json_body=[1], who="admin")
    assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"


# ------------------------------------------------------------ smaller routes

def test_notifications_read_refuses_ids_that_are_not_ids(world):
    for body, code in (({"ids": [HUGE]}, "invalid_ids_list"), ({"ids": ["1"]}, "invalid_ids_list"),
                       ([1], "body_must_be_object")):
        resp = send(world, "POST", "/api/notifications/read", json_body=body)
        assert resp.status_code == 400 and code_of(resp) == code, body
    assert send(world, "POST", "/api/notifications/read", content_type=None).status_code == 200


def test_admin_temperatures_and_timezone_refuse_a_body_that_is_not_an_object(world):
    resp = send(world, "POST", "/admin/llm-temperatures", json_body=[1], who="admin")
    assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"
    resp = send(world, "POST", "/api/user/timezone", json_body=["Europe/Berlin"])
    assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"
    assert send(world, "POST", "/api/user/timezone", content_type=None).status_code == 200


@pytest.mark.parametrize("body,code", [
    ({"recording_ids": [1, 2], "delete_originals": "maybe"}, "invalid_boolean_flag"),
    ({"recording_ids": [True, 1]}, "invalid_recording_ids"),
    ({"recording_ids": [HUGE, 1]}, "invalid_recording_ids"),
    ([1, 2], "body_must_be_object"),
], ids=repr)
def test_merge_refuses_fields_of_the_wrong_type(world, body, code):
    resp = send(world, "POST", "/api/recordings/merge", json_body=body)
    assert resp.status_code == 400 and code_of(resp) == code


def test_merge_reads_the_string_false_as_keep():
    from src.utils.request_body import flag_fields
    flags, error = flag_fields({"delete_originals": "false"}, ("delete_originals",))
    assert error is None and flags == {"delete_originals": False}


def test_summary_prompt_and_translation_refuse_a_body_that_is_not_an_object(world):
    rec = world["rec"]
    resp = send(world, "POST", f"/recording/{rec}/summary_prompt", json_body=[1])
    assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"
    resp = send(world, "POST", f"/recording/{rec}/summary_prompt",
                json_body={"target": "tag", "id": HUGE, "prompt": "x"})
    assert resp.status_code < 500


@pytest.mark.parametrize("field", ["group_id", "naming_template_id", "export_template_id"])
@pytest.mark.parametrize("bad", [HUGE, [1], 2.5, True], ids=repr)
def test_folder_and_tag_writes_refuse_ids_that_are_not_ids(world, field, bad):
    resp = send(world, "POST", "/api/folders", json_body={"name": "F", field: bad})
    assert resp.status_code == 400 and code_of(resp) == "invalid_integer_value", resp.get_data(as_text=True)[:200]
    resp = send(world, "POST", "/api/tags", json_body={"name": "T", field: bad})
    assert resp.status_code == 400 and code_of(resp) == "invalid_integer_value"
    resp = send(world, "PUT", f"/api/tags/{world['a']}", json_body={field: bad})
    assert resp.status_code == 400 and code_of(resp) == "invalid_integer_value"


def test_a_huge_parent_id_is_an_invalid_parent(world):
    resp = send(world, "POST", "/api/folders", json_body={"name": "F", "parent_id": HUGE})
    assert resp.status_code == 400 and code_of(resp) == "folder_parent_invalid"


def test_api_v1_routes_refuse_a_body_that_is_not_an_object(world):
    rec = world["rec"]
    for method, path in (("PUT", f"/api/v1/recordings/{rec}/external-refs?system=crm"),
                         ("PUT", f"/api/v1/recordings/{rec}/tags")):
        resp = send(world, method, path, json_body=[1])
        assert resp.status_code == 400 and code_of(resp) == "body_must_be_object", path
    with patch.dict(os.environ, {"ENABLE_INQUIRE_MODE": "true"}):
        resp = send(world, "POST", "/api/v1/inquire", json_body=["why"])
    assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"
