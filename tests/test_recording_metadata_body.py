"""Recording metadata writes refuse a body that is not an object (#654) and
a meeting_date that is neither a string nor null (#655).

SHARED-DB: the users, recording, tags, speaker, group and tokens made here
are removed afterwards.
"""

import os
import sys
from datetime import datetime
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording
from tests.request_body_support import code_of, make_world, send


@pytest.fixture
def world():
    with make_world() as ids, patch("src.api.recordings.export_recording"):
        yield ids


def _rec(world):
    with app.app_context():
        return db.session.get(Recording, world["rec"])


def _patch(world, body):
    return send(world, "PATCH", f"/api/v1/recordings/{world['rec']}", json_body=body)


# ------------------------------------------------------------------ #654

@pytest.mark.parametrize("body", [42, True, 3.5, "abc", ["x"]])
def test_patch_rejects_a_non_object_body(world, body):
    resp = _patch(world, body)
    assert resp.status_code == 400
    assert code_of(resp) == "body_must_be_object"
    assert _rec(world).title == "Sync"


@pytest.mark.parametrize("body", [{}, [], 0, False, ""])
def test_patch_keeps_no_data_for_falsy_bodies(world, body):
    resp = _patch(world, body)
    assert resp.status_code == 400
    assert code_of(resp) == "no_data_provided"


def test_patch_accepts_a_valid_metadata_object(world):
    resp = _patch(world, {"title": "Renamed", "participants": "Dana, Omar"})
    assert resp.status_code == 200
    assert resp.get_json()["recording"]["title"] == "Renamed"
    assert _rec(world).participants == "Dana, Omar"
    resp = _patch(world, {"title": 5})
    assert resp.status_code == 400 and code_of(resp) == "invalid_parameter"


@pytest.mark.parametrize("route", ["notes", "summary"])
def test_notes_and_summary_reject_a_non_object_body(world, route):
    path = f"/api/v1/recordings/{world['rec']}/{route}"
    for raw in ("42", '"abc"', "[1]", "true"):
        resp = send(world, "PUT", path, data=raw)
        assert resp.status_code == 400 and code_of(resp) == "body_must_be_object", raw
    resp = send(world, "PUT", path, json_body={})
    assert resp.status_code == 400 and code_of(resp) == f"{route}_field_required"
    resp = send(world, "PUT", path, json_body={route: "text"})
    assert resp.status_code == 200 and resp.get_json()[route] == "text"


@pytest.mark.parametrize("raw", ["42", '"abc"', '["x"]', "true"])
def test_save_rejects_a_non_object_body(world, raw):
    resp = send(world, "POST", "/save", data=raw)
    assert resp.status_code == 400 and code_of(resp) == "body_must_be_object"


@pytest.mark.parametrize("data,ctype,status,code", [
    ("", "application/json", 400, "no_data_provided"),
    ("{not json", "application/json", 400, "invalid_json"),
    ('{"id": 1}', "text/plain", 415, "invalid_json"),
    ("{}", "application/json", 400, "no_data_provided"),
    ("[]", "application/json", 400, "no_data_provided"),
])
def test_save_rejects_unparsable_bodies(world, data, ctype, status, code):
    resp = send(world, "POST", "/save", data=data, content_type=ctype)
    assert resp.status_code == status
    assert code_of(resp) == code
    assert "Traceback" not in resp.get_data(as_text=True)


def test_save_still_updates_a_recording(world):
    resp = send(world, "POST", "/save", json_body={"id": world["rec"], "notes": "n", "title": "Saved"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    rec = _rec(world)
    assert rec.notes == "n" and rec.title == "Saved"


def test_save_logs_the_recording_id_when_it_fails(world):
    with patch("src.api.recordings.audit.edited", side_effect=RuntimeError("boom")):
        resp = send(world, "POST", "/save", json_body={"id": world["rec"], "notes": "n"})
    assert resp.status_code == 500 and code_of(resp) == "unexpected_error_saving"


# ------------------------------------------------------------------ #655

@pytest.mark.parametrize("value", [123, 1.5, True, [1], {"a": 1}, {}, [], False, 0])
def test_patch_rejects_non_string_meeting_date(world, value):
    with app.app_context():
        db.session.get(Recording, world["rec"]).meeting_date = datetime(2026, 1, 2, 3, 4)
        db.session.commit()
    resp = _patch(world, {"meeting_date": value})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_parameter"
    assert _rec(world).meeting_date == datetime(2026, 1, 2, 3, 4)


def test_patch_meeting_date_strings_and_null_still_work(world):
    resp = _patch(world, {"meeting_date": "2026-07-02T18:00:00+02:00"})
    assert resp.status_code == 200
    assert _rec(world).meeting_date == datetime(2026, 7, 2, 16, 0)
    resp = _patch(world, {"meeting_date": "2026-07-02T11:30:00"})
    assert resp.status_code == 200
    assert _rec(world).meeting_date == datetime(2026, 7, 2, 11, 30)
    resp = _patch(world, {"meeting_date": None})
    assert resp.status_code == 200
    assert _rec(world).meeting_date is None
    resp = _patch(world, {"meeting_date": "not-a-date"})
    assert resp.status_code == 400 and code_of(resp) == "invalid_meeting_date"


@pytest.mark.parametrize("value", [123, True, [1], {}])
def test_save_rejects_non_string_meeting_date(world, value):
    resp = send(world, "POST", "/save", json_body={"id": world["rec"], "meeting_date": value})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_parameter"


def test_save_accepts_a_string_meeting_date(world):
    resp = send(world, "POST", "/save", json_body={"id": world["rec"], "meeting_date": "2026-07-02T11:30:00"})
    assert resp.status_code == 200
    assert _rec(world).meeting_date == datetime(2026, 7, 2, 11, 30)
