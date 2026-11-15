"""API v1: the translation fields of a recording (issue #614).

GET /api/v1/recordings/<id> always reports transcription_translation_language.
The translation text is returned as transcription_translation only when
translation is named in include, so the default response stays the size it
was. List entries report has_translation.

SHARED-DB: the user, its token and its recordings are removed afterwards.
"""

import json
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.api.api_v1 import _openapi_with_scopes
from src.models import APIToken, Recording, User
from src.utils.token_auth import hash_token

SEGMENTS = json.dumps([{"speaker": "SPEAKER_00", "sentence": "Hello everyone", "start_time": 0.0, "end_time": 1.5}])
TRANSLATED = json.dumps([{"speaker": "SPEAKER_00", "sentence": "Bonjour tout le monde", "start_time": 0.0,
                          "end_time": 1.5}])


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        user = User(username=f"tr_{s}", email=f"tr_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        plain = Recording(user_id=user.id, title="Plain", status="COMPLETED", original_filename="p.wav",
                          transcription=SEGMENTS, summary="Summary text")
        translated = Recording(user_id=user.id, title="Translated", status="COMPLETED", original_filename="t.wav",
                               transcription=SEGMENTS, summary="Summary text",
                               transcription_translation=TRANSLATED,
                               transcription_translation_language="French", translation_duration_seconds=4)
        db.session.add_all([plain, translated])
        token = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add(APIToken(user_id=user.id, token_hash=hash_token(token), name="tr"))
        db.session.commit()
        ids = dict(user=user.id, plain=plain.id, translated=translated.id, token=token)
    yield ids
    with app.app_context():
        db.session.rollback()
        Recording.query.filter_by(user_id=ids["user"]).delete(synchronize_session=False)
        APIToken.query.filter_by(user_id=ids["user"]).delete(synchronize_session=False)
        User.query.filter_by(id=ids["user"]).delete(synchronize_session=False)
        db.session.commit()


def _get(world, path):
    response = app.test_client().get(path, headers={"Authorization": f"Bearer {world['token']}"})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_default_detail_has_the_language_and_no_translation_text(world):
    body = _get(world, f"/api/v1/recordings/{world['translated']}")
    assert body["transcription_translation_language"] == "French"
    assert body["translation_duration_seconds"] == 4
    assert "transcription_translation" not in body
    assert "Bonjour" not in json.dumps(body)
    # The default large fields are unchanged.
    assert "Hello everyone" in body["transcription"]
    assert body["summary"] == "Summary text"
    assert "notes" in body


def test_include_translation_returns_the_formatted_text(world):
    body = _get(world, f"/api/v1/recordings/{world['translated']}?include=transcription,summary,notes,translation")
    assert "Bonjour tout le monde" in body["transcription_translation"]
    assert body["transcription_translation_language"] == "French"
    # Same template as the transcription: only the sentence differs.
    assert body["transcription_translation"] == body["transcription"].replace("Hello everyone",
                                                                              "Bonjour tout le monde")
    assert body["summary"] == "Summary text"


def test_include_translation_alone_returns_only_that_large_field(world):
    body = _get(world, f"/api/v1/recordings/{world['translated']}?include=translation")
    assert "Bonjour tout le monde" in body["transcription_translation"]
    assert "transcription" not in body and "summary" not in body and "notes" not in body


def test_recording_without_translation_returns_null(world):
    body = _get(world, f"/api/v1/recordings/{world['plain']}")
    assert body["transcription_translation_language"] is None
    assert "transcription_translation" not in body
    body = _get(world, f"/api/v1/recordings/{world['plain']}?include=translation")
    assert body["transcription_translation"] is None
    assert body["transcription_translation_language"] is None


def test_minimal_format_omits_the_text_and_keeps_the_language(world):
    body = _get(world, f"/api/v1/recordings/{world['translated']}?format=minimal&include=translation")
    assert "transcription_translation" not in body
    assert body["transcription_translation_language"] == "French"


def test_list_entries_report_has_translation(world):
    items = {r["id"]: r for r in _get(world, "/api/v1/recordings?per_page=100")["recordings"]}
    assert items[world["translated"]]["has_translation"] is True
    assert items[world["plain"]]["has_translation"] is False
    for item in items.values():
        assert item["has_transcription"] is True and item["has_summary"] is True
        assert "transcription_translation" not in item


def test_transcript_endpoint_still_serves_the_translation(world):
    body = _get(world, f"/api/v1/recordings/{world['translated']}/transcript?translation=true")
    assert body["translation"] is True and body["translation_language"] == "French"


def test_openapi_documents_the_fields():
    with app.app_context():
        spec = _openapi_with_scopes()
    text = json.dumps(spec)
    for name in ("has_translation", "transcription_translation_language", "transcription_translation"):
        assert f'"{name}"' in text
    include = [p for p in spec["paths"]["/recordings/{id}"]["get"]["parameters"] if p["name"] == "include"][0]
    assert "translation" in include["description"]
