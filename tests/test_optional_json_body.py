"""Routes with an optional JSON body accept a request without a body (#621).

A request without a body is handled like {}. A body that is present is
still parsed strictly, so a wrong content type or malformed JSON is refused
and no field a client sent is dropped silently.

SHARED-DB: the user, token and recording are removed afterwards.
"""

import os
import secrets
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, User
from src.services.job_queue import job_queue
from src.utils.request_body import optional_json_object
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False

NO_BODY = [("", "application/json"), ("", None)]


@pytest.fixture
def world():
    with app.app_context():
        n = f"optbody_{secrets.token_hex(4)}"
        u = User(username=n, email=f"{n}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                      audio_path="local://s.wav", transcription="Speaker 1: a real transcript with content.")
        plain = f"tok-{secrets.token_urlsafe(16)}"
        t = APIToken(user_id=u.id, token_hash=hash_token(plain), name="optbody")
        db.session.add_all([r, t])
        db.session.commit()
        ids = dict(user=u.id, rec=r.id, token=t.id, plain=plain)
    yield ids
    with app.app_context():
        db.session.rollback()
        for model, key in ((APIToken, "token"), (Recording, "rec"), (User, "user")):
            obj = db.session.get(model, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _send(world, method, path, data, content_type):
    with app.test_client() as c:
        return c.open(path, method=method, data=data, content_type=content_type,
                      headers={"Authorization": f"Bearer {world['plain']}"})


# ------------------------------------------------------------------ helper

@pytest.mark.parametrize("data,content_type", NO_BODY)
def test_no_body_is_an_empty_object(data, content_type):
    with app.test_request_context("/x", method="POST", data=data, content_type=content_type):
        assert optional_json_object() == {}


def test_a_json_object_is_returned():
    with app.test_request_context("/x", method="POST", data='{"a": 1}', content_type="application/json"):
        assert optional_json_object() == {"a": 1}


def test_json_null_is_an_empty_object():
    with app.test_request_context("/x", method="POST", data="null", content_type="application/json"):
        assert optional_json_object() == {}


@pytest.mark.parametrize("data,content_type,status", [
    ('{"a": 1}', "text/plain", 415),
    ('{"a": 1}', None, 415),
    ("{not json", "application/json", 400),
    ("[1, 2]", "application/json", 400),
])
def test_a_body_that_cannot_be_used_is_refused(data, content_type, status):
    from werkzeug.exceptions import HTTPException
    with app.test_request_context("/x", method="POST", data=data, content_type=content_type):
        with pytest.raises(HTTPException) as exc:
            optional_json_object()
    assert exc.value.code == status


# ------------------------------------------------------------------ routes

@pytest.mark.parametrize("data,content_type", NO_BODY + [("{}", "application/json")])
def test_v1_summarize_without_a_body_queues_a_job(world, data, content_type):
    with patch("src.services.llm.client", object()), patch.object(job_queue, "enqueue", return_value=11):
        resp = _send(world, "POST", f"/api/v1/recordings/{world['rec']}/summarize", data, content_type)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert resp.get_json()["success"] is True and resp.get_json()["job_id"] == 11


@pytest.mark.parametrize("data,content_type", NO_BODY)
def test_v1_transcribe_without_a_body_reaches_its_checks(world, data, content_type):
    storage = MagicMock()
    storage.exists.return_value = False
    with patch("src.services.storage.get_storage_service", return_value=storage), \
            patch.object(job_queue, "enqueue") as enq:
        resp = _send(world, "POST", f"/api/v1/recordings/{world['rec']}/transcribe", data, content_type)
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "reprocess_audio_not_found"
    enq.assert_not_called()


@pytest.mark.parametrize("data,content_type,status,code", [
    ("", "application/json", 400, "tag_ids_required"),
    ("", None, 400, "tag_ids_required"),
    ('{"tag_ids": []}', "text/plain", 415, None),
])
def test_v1_tags_without_a_body_gives_the_json_validation_error(world, data, content_type, status, code):
    resp = _send(world, "POST", f"/api/v1/recordings/{world['rec']}/tags", data, content_type)
    assert resp.status_code == status
    if code:
        assert resp.get_json()["code"] == code


@pytest.mark.parametrize("data,content_type", NO_BODY)
def test_token_rename_without_a_body_gives_name_required(world, data, content_type):
    resp = _send(world, "PATCH", f"/api/tokens/{world['token']}", data, content_type)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "name_required"


def _session(world):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(world["user"])
        s["_fresh"] = True
    return c


@pytest.mark.parametrize("data,content_type", NO_BODY)
def test_web_reprocess_summary_without_a_body_queues_a_job(world, data, content_type):
    with patch("src.services.llm_config.is_configured", return_value=True), patch("src.services.llm.client", object()), \
            patch.object(job_queue, "enqueue", return_value=12) as enq:
        resp = _session(world).post(f"/recording/{world['rec']}/reprocess_summary", data=data,
                                    content_type=content_type)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    assert enq.call_args.kwargs["job_type"] == "reprocess_summary"


def test_web_reprocess_summary_with_malformed_json_is_a_client_error(world):
    with patch("src.services.llm_config.is_configured", return_value=True), patch("src.services.llm.client", object()), \
            patch.object(job_queue, "enqueue") as enq:
        resp = _session(world).post(f"/recording/{world['rec']}/reprocess_summary", data="{not json",
                                    content_type="application/json")
    assert resp.status_code == 400
    enq.assert_not_called()
