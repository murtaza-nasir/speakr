"""The same uploaded bytes are a different recording when the kept span differs.

The duplicate warning and voice-recorder reuse match the file hash and the
resolved kept span. An API idempotency key still returns the first recording
for 24 hours, whatever range the retry names. The upload cut and a later trim
in the transcript editor leave that key as the client sent it.
"""
import io
import json
import shutil

import pytest

from src.api import api_v1
from src.app import app
from src.database import db
from src.models import APIToken, Recording
from src.services import recording_trim

from tests.test_upload_conversion_job import (  # noqa: F401
    _run_convert_job, user,
)
from tests.test_upload_trim import _post_api, _post_upload, _token, _two_tone_mp3

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are not installed",
)


@pytest.fixture(autouse=True)
def _convert_workers():
    from unittest.mock import patch
    with patch("src.services.recording_ingestion.CONVERT_WORKERS", 1):
        yield


def _count(uid):
    return Recording.query.filter_by(user_id=uid).count()


def test_the_same_bytes_with_another_kept_span_are_a_new_recording(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        first = _post_upload(user, payload, "two.mp3", {"trim_start": "1", "trim_end": "4"})
        assert first.status_code == 202
        first_id = first.get_json()["id"]

        second = _post_upload(user, payload, "two.mp3", {"trim_start": "3", "trim_end": "5"})
        assert second.status_code == 202
        body = second.get_json()
        assert body["id"] != first_id
        assert "duplicate_warning" not in body
        assert _count(user) == 2
        rec = db.session.get(Recording, body["id"])
        assert (rec.upload_trim_start, rec.upload_trim_end) == (3, 5)
        assert rec.file_hash == db.session.get(Recording, first_id).file_hash


def test_the_same_bytes_with_the_same_kept_span_warn_about_the_duplicate(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        first = _post_upload(user, payload, "two.mp3", {"trim_start": "2", "trim_end": "4"})
        assert first.status_code == 202
        first_id = first.get_json()["id"]

        second = _post_upload(user, payload, "two.mp3", {"trim_start": "2", "trim_end": "4"})
        assert second.status_code == 202
        body = second.get_json()
        assert body["id"] != first_id
        assert body["duplicate_warning"]["existing_recording_id"] == first_id
        assert _count(user) == 2


def test_voice_recorder_reuses_only_the_same_bytes_and_span(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "voice.mp3")
    with app.app_context():
        token = _token(user)
        client = app.test_client()
        saved = api_v1.limiter
        api_v1.limiter = None
        try:
            def post(fields):
                data = {"secret": token, "file": (io.BytesIO(payload), "voice.mp3")}
                data.update(fields)
                return client.post(
                    "/api/v1/integrations/asr-voice-recorder/upload",
                    data=data, content_type="multipart/form-data",
                )

            first = post({"trim_start": "1", "trim_end": "4"})
            assert first.status_code == 200, first.get_json()
            first_id = first.get_json()["id"]
            assert first.get_json()["idempotent_replay"] is False

            same = post({"trim_start": "1", "trim_end": "4"})
            assert same.status_code == 200
            assert same.get_json()["id"] == first_id
            assert same.get_json()["idempotent_replay"] is True
            assert _count(user) == 1

            other = post({"trim_start": "3", "trim_end": "5"})
            assert other.status_code == 200, other.get_json()
            assert other.get_json()["id"] != first_id
            assert other.get_json()["idempotent_replay"] is False
            assert _count(user) == 2
        finally:
            api_v1.limiter = saved
            APIToken.query.filter_by(user_id=user).delete()
            db.session.commit()


def test_the_same_idempotency_key_returns_the_first_recording(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        token = _token(user)
        try:
            first = _post_api(token, payload, "two.mp3", {
                "idempotency_key": "span-key", "trim_start": "1", "trim_end": "4",
            })
            assert first.status_code == 202
            first_id = first.get_json()["id"]

            retry = _post_api(token, payload, "two.mp3", {
                "idempotency_key": "span-key", "trim_start": "3", "trim_end": "5",
            })
            assert retry.status_code == 200
            body = retry.get_json()
            assert body["id"] == first_id
            assert body["idempotent_replay"] is True
            assert _count(user) == 1
            rec = db.session.get(Recording, first_id)
            assert rec.upload_idempotency_key == "span-key"
            assert (rec.upload_trim_start, rec.upload_trim_end) == (1, 4)
        finally:
            APIToken.query.filter_by(user_id=user).delete()
            db.session.commit()


def test_a_new_key_or_no_key_with_another_range_creates_a_recording(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        token = _token(user)
        try:
            first = _post_api(token, payload, "two.mp3", {
                "idempotency_key": "span-key", "trim_start": "1", "trim_end": "4",
            })
            assert first.status_code == 202
            first_id = first.get_json()["id"]

            new_key = _post_api(token, payload, "two.mp3", {
                "idempotency_key": "other-key", "trim_start": "3", "trim_end": "5",
            })
            assert new_key.status_code == 202
            new_id = new_key.get_json()["id"]
            assert new_id != first_id
            assert "idempotent_replay" not in new_key.get_json()

            no_key = _post_api(token, payload, "two.mp3", {"trim_start": "2", "trim_end": "5"})
            assert no_key.status_code == 202
            assert no_key.get_json()["id"] not in (first_id, new_id)
            assert _count(user) == 3
            stored = {
                r.id: r.upload_idempotency_key
                for r in Recording.query.filter_by(user_id=user)
            }
            assert stored[first_id] == "span-key"
            assert stored[new_id] == "other-key"
            assert stored[no_key.get_json()["id"]] is None
        finally:
            APIToken.query.filter_by(user_id=user).delete()
            db.session.commit()


def test_a_later_trim_does_not_change_the_idempotency_key(user, tmp_path):
    payload = _two_tone_mp3(tmp_path / "two.mp3")
    with app.app_context():
        token = _token(user)
        try:
            res = _post_api(token, payload, "two.mp3", {
                "idempotency_key": "span-key", "trim_start": "1", "trim_end": "5",
            })
            assert res.status_code == 202
            rid = res.get_json()["id"]
            assert db.session.get(Recording, rid).upload_idempotency_key == "span-key"

            assert _run_convert_job().status == "completed"
            rec = db.session.get(Recording, rid)
            assert rec.upload_idempotency_key == "span-key"
            assert (rec.upload_trim_start, rec.upload_trim_end) == (1, 5)

            rec.status = "COMPLETED"
            rec.transcription = json.dumps([
                {"speaker": "SPEAKER_00", "sentence": "Hello.", "start_time": 0.2, "end_time": 1.5},
                {"speaker": "SPEAKER_00", "sentence": "There.", "start_time": 1.6, "end_time": 3.2},
            ])
            db.session.commit()
            before = rec.audio_path
            recording_trim.run_trim_job(rec, {
                "start": 0.5, "end": 2.5, "source_audio_path": before,
            })
            db.session.refresh(rec)
            assert rec.audio_path != before
            assert rec.upload_idempotency_key == "span-key"
            assert (rec.upload_trim_start, rec.upload_trim_end) == (1, 5)
        finally:
            APIToken.query.filter_by(user_id=user).delete()
            db.session.commit()
