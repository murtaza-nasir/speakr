"""The prompts and processing defaults of a folder or a tag are checked (#687).

custom_prompt, title_prompt, default_hotwords and default_initial_prompt
are strings or null; default_language (String(10)) and
default_transcription_model (String(120)) are strings that fit their
column; default_min_speakers, default_max_speakers and retention_days are
whole numbers in range (a digit string from a form is read as one, '' as
none); protect_from_deletion, auto_share_on_apply, share_with_group_lead
and is_auto_process are flags. Anything else answers 400 before any change.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards, the folders made here too.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Folder, Tag
from src.utils.request_body import PROCESSING_FIELDS, clean_fields
from tests.request_body_support import code_of, make_world, send

BAD = [
    ("custom_prompt", 5, "field_must_be_string"),
    ("title_prompt", ["x"], "field_must_be_string"),
    ("default_hotwords", {"a": 1}, "field_must_be_string"),
    ("default_initial_prompt", 5, "field_must_be_string"),
    ("default_language", "x" * 11, "field_too_long"),
    ("default_language", 5, "field_must_be_string"),
    ("default_transcription_model", "m" * 121, "field_too_long"),
    ("default_min_speakers", "many", "field_out_of_range"),
    ("default_min_speakers", 2 ** 63, "field_out_of_range"),
    ("default_max_speakers", 2.5, "field_out_of_range"),
    ("default_max_speakers", True, "field_out_of_range"),
    ("retention_days", -2, "field_out_of_range"),
    ("retention_days", 10 ** 9, "field_out_of_range"),
    ("protect_from_deletion", "maybe", "invalid_boolean_flag"),
    ("auto_share_on_apply", 2, "invalid_boolean_flag"),
    ("share_with_group_lead", None, "invalid_boolean_flag"),
]


@pytest.fixture
def world():
    with make_world() as ids, \
            patch("src.api.folders.SystemSetting.get_setting", return_value=True):
        try:
            yield ids
        finally:
            with app.app_context():
                Folder.query.filter_by(user_id=ids["owner"]).delete()
                db.session.commit()


def test_clean_fields_normalises_form_values():
    data = {"default_min_speakers": "2", "default_max_speakers": "", "retention_days": "-1",
            "protect_from_deletion": "false", "custom_prompt": None}
    assert clean_fields(data, PROCESSING_FIELDS) is None
    assert data == {"default_min_speakers": 2, "default_max_speakers": None, "retention_days": -1,
                    "protect_from_deletion": False, "custom_prompt": None}


@pytest.mark.parametrize("field,value,code", BAD, ids=lambda v: repr(v)[:20])
def test_folder_and_tag_writes_refuse_a_bad_processing_field(world, field, value, code):
    for method, path in (("POST", "/api/folders"), ("POST", "/api/tags"),
                         ("PUT", f"/api/tags/{world['a']}"), ("POST", "/api/v1/tags"),
                         ("PUT", f"/api/v1/tags/{world['a']}")):
        body = {field: value} if method == "PUT" else {"name": "Proc", field: value}
        resp = send(world, method, path, json_body=body)
        assert resp.status_code == 400, (path, resp.status_code, resp.get_data(as_text=True)[:200])
        assert code_of(resp) == code, path
    with app.app_context():
        assert Folder.query.filter_by(user_id=world["owner"], name="Proc").count() == 0
        assert Tag.query.filter_by(user_id=world["owner"], name="Proc").count() == 0


def test_folder_update_refuses_a_bad_processing_field_and_keeps_the_folder(world):
    resp = send(world, "POST", "/api/folders", json_body={"name": "Keep", "default_min_speakers": "2"})
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:200]
    folder_id = resp.get_json()["id"]
    with app.app_context():
        assert db.session.get(Folder, folder_id).default_min_speakers == 2
    resp = send(world, "PUT", f"/api/folders/{folder_id}", json_body={"retention_days": "forever"})
    assert resp.status_code == 400 and code_of(resp) == "field_out_of_range"
    resp = send(world, "PUT", f"/api/folders/{folder_id}", json_body={"custom_prompt": 5})
    assert resp.status_code == 400 and code_of(resp) == "field_must_be_string"
    resp = send(world, "PUT", f"/api/folders/{folder_id}", json_body={"default_language": "de"})
    assert resp.status_code == 200
