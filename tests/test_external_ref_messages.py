"""The messages of a refused external reference are localized (#687 follow-up).

Each RefError names a server.apiError key; the answer keeps its stable
code (invalid_parameter, or conflict for the limit) and says the message
in the interface language.

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User
from src.services import external_refs as xr
from tests.request_body_support import make_world, send

EVENT = {"system": "crm", "kind": "event", "ref": "42"}


@pytest.fixture
def world():
    with make_world() as ids:
        yield ids


def _language(world, code):
    with app.app_context():
        db.session.get(User, world["owner"]).ui_language = code
        db.session.commit()


@pytest.mark.parametrize("raw,key", [
    ([1], "external_ref_not_object"),
    ({**EVENT, "system": "Bad System"}, "external_ref_system_invalid"),
    ({**EVENT, "kind": ""}, "external_ref_kind_invalid"),
    ({**EVENT, "ref": ""}, "external_ref_ref_invalid"),
    ({**EVENT, "url": "ftp://x"}, "external_ref_url_invalid"),
    ({**EVENT, "label": "x" * 201}, "external_ref_label_invalid"),
], ids=lambda v: repr(v)[:24])
def test_each_refusal_names_its_message(raw, key):
    with app.test_request_context():
        with pytest.raises(xr.RefError) as info:
            xr.validate(raw)
    assert info.value.key == key and info.value.code == "invalid_parameter"


def test_a_list_over_the_limit_is_a_conflict_with_the_number():
    with app.test_request_context():
        with pytest.raises(xr.RefError) as info:
            xr.validate_list([EVENT] * (xr.MAX_PER_RECORDING + 1))
    assert info.value.code == "conflict" and info.value.status == 409
    assert str(xr.MAX_PER_RECORDING) in str(info.value)


def test_the_answer_is_in_the_interface_language(world):
    path = f"/api/v1/recordings/{world['rec']}/external-refs"
    resp = send(world, "POST", path, json_body={**EVENT, "system": "Bad System"})
    assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_parameter"
    assert resp.get_json()["error"].startswith("system must be")
    _language(world, "de")
    resp = send(world, "POST", path, json_body={**EVENT, "system": "Bad System"})
    assert resp.status_code == 400 and resp.get_json()["error"].startswith("system muss")
    resp = send(world, "PUT", path, json_body={"external_refs": []})
    assert resp.status_code == 400 and resp.get_json()["error"] == "?system= ist erforderlich"
    resp = send(world, "PUT", path + "?system=crm", json_body={"external_refs": "x"})
    assert resp.status_code == 400 and resp.get_json()["error"] == "external_refs muss eine Liste sein"
