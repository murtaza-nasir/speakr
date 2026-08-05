"""Integration coverage for the ASR Voice Recorder upload adapter."""

import io
import json
import os
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.app import app, db
from src.api import api_v1
from src.models import APIToken, ProcessingJob, Recording, User
from src.utils.ffprobe import FFProbeError
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False


class _StoredObject:
    def __init__(self, locator):
        self.locator = locator


class _Storage:
    def __init__(self, staging_dir):
        self.staging_dir = staging_dir
        self.uploaded = []

    def get_staging_dir(self):
        os.makedirs(self.staging_dir, exist_ok=True)
        return self.staging_dir

    def build_recording_key(self, original_filename, recording_id=None, *, now=None):
        return f"recordings/asr/{recording_id}/{original_filename}"

    def upload_local_file(self, local_path, key, *, content_type=None, delete_source=False):
        self.uploaded.append((local_path, key))
        return _StoredObject(f"local://{key}")


def _user(prefix="asr"):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _token(user, *, expired=False, revoked=False):
    plaintext = f"asr-{secrets.token_urlsafe(24)}"
    token = APIToken(
        user_id=user.id,
        token_hash=hash_token(plaintext),
        name="ASR Voice Recorder",
        revoked=revoked,
        expires_at=(datetime.utcnow() - timedelta(minutes=1)) if expired else None,
    )
    db.session.add(token)
    db.session.commit()
    return token, plaintext


def _login(client, user):
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True


def _cleanup_user(user):
    ProcessingJob.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Recording.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    APIToken.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()


@contextmanager
def _upload_mocks(staging_dir):
    storage = _Storage(staging_dir)

    def convert(filepath, **kwargs):
        result = MagicMock()
        result.output_path = filepath
        result.was_converted = False
        result.was_compressed = False
        result.original_codec = "aac"
        result.final_codec = "aac"
        result.size_reduction_percent = 0
        return result

    def enqueue(**kwargs):
        job = ProcessingJob(
            user_id=kwargs["user_id"],
            recording_id=kwargs["recording_id"],
            job_type=kwargs["job_type"],
            status="completed",
            params=json.dumps(kwargs.get("params") or {}),
            is_new_upload=kwargs.get("is_new_upload", False),
        )
        db.session.add(job)
        db.session.commit()
        return job.id

    with patch("src.api.recordings.get_storage_service", return_value=storage), \
         patch("src.api.recordings.get_codec_info", return_value={"has_video": False, "audio_codec": "aac"}), \
         patch("src.api.recordings.convert_if_needed", side_effect=convert), \
         patch("src.api.recordings.get_duration", return_value=12.5), \
         patch("src.api.recordings.get_creation_date", return_value=None), \
         patch("src.services.job_queue.job_queue.enqueue", side_effect=enqueue) as enqueue_mock, \
         patch("src.services.webhook_dispatch.emit_webhook_event") as webhook_mock:
        yield storage, enqueue_mock, webhook_mock


def _post(client, secret=None, payload=b"audio-bytes", filename="voice.m4a", **fields):
    data = dict(fields)
    if secret is not None:
        data["secret"] = secret
    data["file"] = (io.BytesIO(payload), filename)
    return client.post(
        "/api/v1/integrations/asr-voice-recorder/upload",
        data=data,
        content_type="multipart/form-data",
    )


@pytest.fixture(autouse=True)
def _disable_rate_limiter_for_isolated_requests():
    original = api_v1.limiter
    api_v1.limiter = None
    try:
        yield
    finally:
        api_v1.limiter = original


def test_missing_invalid_expired_and_revoked_secrets_share_one_error(tmp_path):
    with app.app_context():
        user = _user("asr_auth")
        _, expired = _token(user, expired=True)
        _, revoked = _token(user, revoked=True)
        client = app.test_client()
        try:
            for secret in (None, "not-a-token", expired, revoked):
                response = _post(client, secret=secret)
                assert response.status_code == 401
                assert response.get_json() == {"error": "Authentication failed"}
            assert Recording.query.filter_by(user_id=user.id).count() == 0
        finally:
            _cleanup_user(user)


def test_oversized_body_is_rejected_before_multipart_authentication():
    with app.app_context():
        client = app.test_client()
        with patch("src.models.SystemSetting.get_setting", return_value=1):
            response = client.post(
                "/api/v1/integrations/asr-voice-recorder/upload",
                data={
                    "secret": "not-a-token",
                    "file": (io.BytesIO(b"x" * (2 * 1024 * 1024)), "large.m4a"),
                },
                content_type="multipart/form-data",
            )
        assert response.status_code == 413
        assert response.get_json() == {"error": "File too large"}


def test_session_or_header_cannot_replace_multipart_secret(tmp_path):
    with app.app_context():
        user = _user("asr_no_fallback")
        _, token = _token(user)
        client = app.test_client()
        _login(client, user)
        try:
            session_response = _post(client, secret=None)
            header_response = client.post(
                "/api/v1/integrations/asr-voice-recorder/upload",
                headers={"Authorization": f"Bearer {token}"},
                data={"file": (io.BytesIO(b"audio"), "voice.m4a")},
                content_type="multipart/form-data",
            )
            assert session_response.status_code == 401
            assert header_response.status_code == 401
        finally:
            _cleanup_user(user)


def test_valid_secret_controls_owner_and_maps_asr_fields(tmp_path, caplog):
    with app.app_context():
        session_user = _user("asr_session")
        token_user = _user("asr_owner")
        token_record, token = _token(token_user)
        client = app.test_client()
        _login(client, session_user)
        try:
            with _upload_mocks(str(tmp_path)) as (storage, enqueue, webhook):
                response = _post(
                    client,
                    secret=token,
                    file_name="../../Interview\r\nOne.m4a",
                    note="Recorded through the RODE receiver",
                    date="1700000000",
                    duration="999999",
                )

            assert response.status_code == 200, response.data
            payload = response.get_json()
            recording = db.session.get(Recording, payload["id"])
            assert recording.user_id == token_user.id
            assert recording.user_id != session_user.id
            assert recording.original_filename == "Interview_One.m4a"
            assert recording.notes == "Recorded through the RODE receiver"
            assert recording.meeting_date == datetime.fromtimestamp(1700000000, tz=timezone.utc).replace(tzinfo=None)
            assert recording.audio_duration_seconds == 12.5
            assert recording.processing_source == "asr_voice_recorder"
            assert payload["idempotent_replay"] is False
            assert len(storage.uploaded) == 1
            assert enqueue.call_count == 1
            assert webhook.call_count == 1
            db.session.refresh(token_record)
            assert token_record.last_used_at is not None
            assert token not in response.get_data(as_text=True)
            assert token not in caplog.text
        finally:
            _cleanup_user(session_user)
            _cleanup_user(token_user)


def test_date_accepts_epoch_milliseconds(tmp_path):
    with app.app_context():
        user = _user("asr_date_ms")
        _, token = _token(user)
        client = app.test_client()
        try:
            with _upload_mocks(str(tmp_path)):
                response = _post(client, secret=token, date="1700000000000")
            recording = db.session.get(Recording, response.get_json()["id"])
            assert recording.meeting_date == datetime.fromtimestamp(1700000000, tz=timezone.utc).replace(tzinfo=None)
        finally:
            _cleanup_user(user)


def test_identical_retry_reuses_recording_without_new_job_or_event(tmp_path):
    with app.app_context():
        user = _user("asr_retry")
        _, token = _token(user)
        client = app.test_client()
        try:
            with _upload_mocks(str(tmp_path)) as (storage, enqueue, webhook):
                first = _post(client, secret=token, payload=b"same-recording")
                second = _post(client, secret=token, payload=b"same-recording")

            assert first.status_code == 200
            assert second.status_code == 200
            first_payload = first.get_json()
            second_payload = second.get_json()
            assert first_payload["id"] == second_payload["id"]
            assert first_payload["idempotent_replay"] is False
            assert second_payload["idempotent_replay"] is True
            assert Recording.query.filter_by(user_id=user.id).count() == 1
            assert len(storage.uploaded) == 1
            assert enqueue.call_count == 1
            assert webhook.call_count == 1
        finally:
            _cleanup_user(user)


def test_failed_replay_job_recovery_cleans_retry_staging_file(tmp_path):
    with app.app_context():
        user = _user("asr_recovery_cleanup")
        _, token = _token(user)
        client = app.test_client()
        try:
            with _upload_mocks(str(tmp_path)) as (_, enqueue, _):
                first = _post(client, secret=token, payload=b"recovery-audio")
                recording = db.session.get(Recording, first.get_json()["id"])
                ProcessingJob.query.filter_by(recording_id=recording.id).delete()
                recording.status = "PENDING"
                db.session.commit()

                for name in os.listdir(tmp_path):
                    os.remove(os.path.join(tmp_path, name))

                enqueue.side_effect = RuntimeError("queue unavailable")
                replay = _post(client, secret=token, payload=b"recovery-audio")

            assert replay.status_code == 500
            assert os.listdir(tmp_path) == []
        finally:
            _cleanup_user(user)


def test_same_bytes_with_different_metadata_are_distinct_recordings(tmp_path):
    with app.app_context():
        user = _user("asr_distinct")
        _, token = _token(user)
        client = app.test_client()
        try:
            with _upload_mocks(str(tmp_path)):
                first = _post(
                    client,
                    secret=token,
                    payload=b"same-audio",
                    file_name="first.m4a",
                    note="First occurrence",
                    date="1700000000",
                )
                second = _post(
                    client,
                    secret=token,
                    payload=b"same-audio",
                    file_name="second.m4a",
                    note="Second occurrence",
                    date="1700003600",
                )
            assert first.get_json()["id"] != second.get_json()["id"]
            assert Recording.query.filter_by(user_id=user.id).count() == 2
        finally:
            _cleanup_user(user)


def test_same_bytes_are_independent_between_users(tmp_path):
    with app.app_context():
        first_user = _user("asr_first")
        second_user = _user("asr_second")
        _, first_token = _token(first_user)
        _, second_token = _token(second_user)
        client = app.test_client()
        try:
            with _upload_mocks(str(tmp_path)):
                first = _post(client, secret=first_token, payload=b"shared-bytes")
                second = _post(client, secret=second_token, payload=b"shared-bytes")
            assert first.get_json()["id"] != second.get_json()["id"]
            assert Recording.query.filter_by(user_id=first_user.id).count() == 1
            assert Recording.query.filter_by(user_id=second_user.id).count() == 1
        finally:
            _cleanup_user(first_user)
            _cleanup_user(second_user)


def test_authenticated_connection_probe_without_file_returns_200():
    with app.app_context():
        user = _user("asr_connection_test")
        _, token = _token(user)
        client = app.test_client()
        try:
            response = client.post(
                "/api/v1/integrations/asr-voice-recorder/upload",
                data={"secret": token, "source": "com.nll.asr"},
                content_type="multipart/form-data",
            )
            assert response.status_code == 200
            assert response.get_json() == {"status": "ok", "connection_test": True}
            assert Recording.query.filter_by(user_id=user.id).count() == 0
        finally:
            _cleanup_user(user)


def test_missing_file_after_valid_auth_returns_400():
    with app.app_context():
        user = _user("asr_missing_file")
        _, token = _token(user)
        client = app.test_client()
        try:
            response = client.post(
                "/api/v1/integrations/asr-voice-recorder/upload",
                data={"secret": token, "file_name": "missing.m4a"},
                content_type="multipart/form-data",
            )
            assert response.status_code == 400
            assert response.get_json() == {"error": "No file provided"}
        finally:
            _cleanup_user(user)


def test_unprobeable_video_named_upload_uses_audio_limit_and_cleans_staging(tmp_path):
    with app.app_context():
        user = _user("asr_unprobeable")
        _, token = _token(user)
        client = app.test_client()
        storage = _Storage(str(tmp_path))

        def setting(name, default=None):
            if name == "max_file_size_mb":
                return 1
            if name == "max_audio_only_video_size_mb":
                return 4
            return default

        try:
            with patch("src.api.recordings.get_storage_service", return_value=storage), \
                 patch("src.api.recordings.SystemSetting.get_setting", side_effect=setting), \
                 patch("src.api.recordings.get_codec_info", side_effect=FFProbeError("unprobeable")), \
                 patch("src.api.recordings.VIDEO_RETENTION", True), \
                 patch("src.api.recordings.chunking_service", None):
                response = _post(
                    client,
                    secret=token,
                    payload=b"x" * (2 * 1024 * 1024),
                    file_name="not-really-video.mp4",
                    filename="not-really-video.mp4",
                )

            assert response.status_code == 413
            assert list(tmp_path.iterdir()) == []
            assert Recording.query.filter_by(user_id=user.id).count() == 0
        finally:
            _cleanup_user(user)


def test_normal_api_upload_still_returns_202(tmp_path):
    with app.app_context():
        user = _user("asr_normal_api")
        _, token = _token(user)
        client = app.test_client()
        try:
            with _upload_mocks(str(tmp_path)):
                response = client.post(
                    "/api/v1/recordings/upload",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file": (io.BytesIO(b"normal-upload"), "normal.m4a")},
                    content_type="multipart/form-data",
                )
            assert response.status_code == 202
            assert "idempotent_replay" not in response.get_json()
        finally:
            _cleanup_user(user)


def test_asr_upload_rate_cost_bounds_requests_and_declared_bytes_before_parsing():
    for declared_mib, expected_cost in ((5, 52), (100, 100)):
        with app.test_request_context(
            "/api/v1/integrations/asr-voice-recorder/upload",
            method="POST",
            environ_overrides={"CONTENT_LENGTH": str(declared_mib * 1024 * 1024)},
        ):
            assert api_v1._asr_upload_rate_cost() == expected_cost

    with app.test_request_context(
        "/api/v1/integrations/asr-voice-recorder/upload",
        method="POST",
    ):
        assert api_v1._asr_upload_rate_cost() == api_v1._ASR_RATE_UNITS_PER_MINUTE


def test_openapi_documents_asr_multipart_contract():
    operation = api_v1.OPENAPI_SPEC["paths"]["/integrations/asr-voice-recorder/upload"]["post"]
    schema = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert operation["security"] == []
    assert schema["required"] == ["secret"]
    assert set(schema["properties"]) == {"file", "file_name", "secret", "date", "duration", "note"}
    assert operation["responses"]["200"]["description"]
    assert api_v1.upload_from_asr_voice_recorder._rate_limit == "520 per minute"
    assert api_v1.upload_from_asr_voice_recorder._rate_limit_options["cost"] is api_v1._asr_upload_rate_cost
