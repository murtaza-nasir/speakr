"""Template writes refuse a regex_patterns value that is not a map of
non-empty strings and an is_default that is not a JSON boolean (#658),
before any row is written or any default is cleared.

SHARED-DB: the users, recording, tags, speaker, group, templates and
tokens made here are removed afterwards.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ExportTemplate, InitialPromptTemplate, NamingTemplate, TranscriptTemplate
from tests.request_body_support import code_of, make_world, send

FAMILIES = [
    ("/api/naming-templates", NamingTemplate),
    ("/api/export-templates", ExportTemplate),
    ("/api/initial-prompt-templates", InitialPromptTemplate),
    ("/api/transcript-templates", TranscriptTemplate),
]


@pytest.fixture
def world():
    with make_world() as ids:
        yield ids


def _count(world, model):
    with app.app_context():
        return model.query.filter_by(user_id=world["owner"]).count()


def _create(world, path, **extra):
    resp = send(world, "POST", path, json_body={"name": "p", "template": "t", **extra})
    assert resp.status_code == 201, resp.get_data(as_text=True)[:300]
    return resp.get_json()["id"]


# ------------------------------------------------------------ regex

@pytest.mark.parametrize("patterns,code", [
    ({"speaker": 123}, "invalid_regex_patterns"),
    ({"speaker": None}, "invalid_regex_patterns"),
    ({"speaker": ["a"]}, "invalid_regex_patterns"),
    ({"speaker": ""}, "invalid_regex_patterns"),
    (["a"], "invalid_regex_patterns"),
    ([], "invalid_regex_patterns"),
    ("(.+)", "invalid_regex_patterns"),
    ({"speaker": "("}, "invalid_regex_pattern"),
])
def test_naming_template_rejects_invalid_regex_patterns(world, patterns, code):
    resp = send(world, "POST", "/api/naming-templates",
                json_body={"name": "p", "template": "t", "regex_patterns": patterns})
    assert resp.status_code == 400 and code_of(resp) == code
    assert _count(world, NamingTemplate) == 0

    tid = _create(world, "/api/naming-templates", regex_patterns={"speaker": "(.+)"})
    resp = send(world, "PUT", f"/api/naming-templates/{tid}", json_body={"regex_patterns": patterns})
    assert resp.status_code == 400 and code_of(resp) == code
    with app.app_context():
        assert json.loads(db.session.get(NamingTemplate, tid).regex_patterns) == {"speaker": "(.+)"}


def test_valid_regex_patterns_round_trip(world):
    resp = send(world, "POST", "/api/naming-templates",
                json_body={"name": "speaker", "template": "{{speaker}}",
                           "regex_patterns": {"speaker": "(.+)", "caller": "^(\\d{3})"}})
    assert resp.status_code == 201
    tid = resp.get_json()["id"]
    with app.app_context():
        assert json.loads(db.session.get(NamingTemplate, tid).regex_patterns) == \
            {"speaker": "(.+)", "caller": "^(\\d{3})"}
    resp = send(world, "PUT", f"/api/naming-templates/{tid}", json_body={"regex_patterns": {"speaker": "(.+?)"}})
    assert resp.status_code == 200
    with app.app_context():
        assert json.loads(db.session.get(NamingTemplate, tid).regex_patterns) == {"speaker": "(.+?)"}
    for empty in (None, {}):
        resp = send(world, "PUT", f"/api/naming-templates/{tid}", json_body={"regex_patterns": empty})
        assert resp.status_code == 200
        with app.app_context():
            assert db.session.get(NamingTemplate, tid).regex_patterns is None


def test_a_stored_non_string_pattern_does_not_break_title_generation(world):
    with app.app_context():
        t = NamingTemplate(user_id=world["owner"], name="old", template="{{x}}-{{filename}}",
                           regex_patterns=json.dumps({"x": 123}))
        db.session.add(t)
        db.session.commit()
        # A row written before #658: the variable is empty, nothing raises
        assert t.apply("meeting.wav") == "-meeting"


# ------------------------------------------------------------ is_default

@pytest.mark.parametrize("path,model", FAMILIES)
@pytest.mark.parametrize("value", ["false", "true", 1, 0, None, [], {}])
def test_template_create_rejects_non_boolean_is_default(world, path, model, value):
    resp = send(world, "POST", path, json_body={"name": "p", "template": "t", "is_default": value})
    assert resp.status_code == 400
    assert code_of(resp) == "invalid_is_default"
    assert _count(world, model) == 0


@pytest.mark.parametrize("path,model", FAMILIES)
def test_template_update_rejects_non_boolean_is_default(world, path, model):
    tid = _create(world, path, is_default=True)
    resp = send(world, "PUT", f"{path}/{tid}", json_body={"is_default": "false"})
    assert resp.status_code == 400 and code_of(resp) == "invalid_is_default"
    with app.app_context():
        assert db.session.get(model, tid).is_default is True


@pytest.mark.parametrize("path,model", FAMILIES)
def test_boolean_is_default_keeps_one_default(world, path, model):
    first = _create(world, path, is_default=True)
    second = _create(world, path, is_default=True)
    with app.app_context():
        defaults = [t.id for t in model.query.filter_by(user_id=world["owner"], is_default=True)]
    assert defaults == [second]
    # An update without the field keeps the flag; false clears it
    resp = send(world, "PUT", f"{path}/{second}", json_body={"name": "renamed"})
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(model, second).is_default is True
        assert db.session.get(model, first).is_default is False
    resp = send(world, "PUT", f"{path}/{first}", json_body={"is_default": True})
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(model, first).is_default is True
        assert db.session.get(model, second).is_default is False
    resp = send(world, "PUT", f"{path}/{first}", json_body={"is_default": False})
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(model, first).is_default is False
