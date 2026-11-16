"""No original file name in the default log.

The storage key of a recording is <prefix>/YYYY/MM/<recording id>/
<timestamp>_<original file name>, and recording.audio_path holds it. Each
test plants a sentinel file name, runs one flow (upload, playback of a
missing file, deletion, retention) with the log captured from DEBUG upward,
and checks that the sentinel is in no record of INFO or higher while the
directory, the timestamp and the extension of the stored file are.

SHARED-DB: the user, recordings and jobs are removed afterwards.
"""

import io
import logging
import os
import re
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, RecordingTombstone, User
from src.services.recording_deletion import delete_recording_completely
from src.services.retention import remove_recording_audio
from src.services.storage import get_storage_service
from src.utils.log_filters import loggable_error, loggable_locator

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

SENTINEL = "ocelot-filename-marker"
FILENAME = f"jane-doe-{SENTINEL}.mp3"
AUDIO = b'RIFF' + b'\x00' * 2048
LABEL = re.compile(r"<stored file local://recordings/\d{4}/\d{2}/(\d+)/\d{14}\.mp3>")


@pytest.fixture
def log(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=app.logger.name)
    return caplog


@pytest.fixture
def user():
    with app.app_context():
        suffix = uuid.uuid4().hex[:10]
        u = User(username=f"lfn_{suffix}", email=f"lfn_{suffix}@local.test", password='x')
        db.session.add(u)
        db.session.commit()
        uid = u.id
        yield uid
        db.session.rollback()
        storage = get_storage_service()
        for rec in Recording.query.filter_by(user_id=uid).all():
            try:
                storage.delete(rec.audio_path, missing_ok=True)
            except Exception:
                pass
        ProcessingJob.query.filter_by(user_id=uid).delete(synchronize_session=False)
        Recording.query.filter_by(user_id=uid).delete(synchronize_session=False)
        RecordingTombstone.query.filter_by(user_id=uid).delete(synchronize_session=False)
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _client(uid):
    from flask import g, has_app_context
    if has_app_context():
        g.pop('_login_user', None)
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    return client


def _upload(uid):
    """POST /upload with ffprobe and ffmpeg stubbed out; the stored recording."""
    def convert(filepath, **kwargs):
        result = MagicMock()
        result.output_path = filepath
        result.was_converted = result.was_compressed = False
        return result

    data = {'file': (io.BytesIO(AUDIO), FILENAME)}
    with patch('src.services.recording_ingestion.get_codec_info',
               return_value={'has_video': False, 'audio_codec': 'mp3', 'duration': 60.0}), \
         patch('src.services.recording_ingestion.convert_if_needed', side_effect=convert), \
         patch('src.services.recording_ingestion.get_duration', return_value=60.0), \
         patch('src.services.recording_ingestion.get_creation_date', return_value=None), \
         patch('src.services.webhook_dispatch.emit_webhook_event'):
        response = _client(uid).post('/upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 202, response.get_data(as_text=True)
    db.session.expire_all()
    recording = Recording.query.filter_by(user_id=uid).one()
    # The test has teeth only while the name is part of the stored path.
    assert SENTINEL in recording.audio_path
    return recording


def _default_log(log):
    return "\n".join(logging.Formatter("%(message)s").format(r) for r in log.records if r.levelno >= logging.INFO)


def _assert_no_name(log):
    default_log = _default_log(log)
    assert SENTINEL not in default_log
    assert "jane-doe" not in default_log
    return default_log


def test_upload_logs_no_file_name_at_info(user, log):
    recording = _upload(user)
    default_log = _assert_no_name(log)
    assert f"recording {recording.id}" in default_log.lower() or str(recording.id) in default_log


def test_deletion_logs_the_stored_file_without_its_name(user, log):
    recording = _upload(user)
    recording_id = recording.id
    log.clear()
    delete_recording_completely(recording)
    db.session.commit()
    default_log = _assert_no_name(log)
    line = next(l for l in default_log.splitlines() if l.startswith(f"Deleted media for recording {recording_id}:"))
    assert LABEL.search(line).group(1) == str(recording_id)


def test_failed_media_deletion_logs_no_file_name(user, log):
    recording = _upload(user)
    recording_id = recording.id
    storage = get_storage_service()
    path = os.path.join(app.config['UPLOAD_FOLDER'], recording.audio_path.split('://', 1)[1])
    log.clear()
    with patch.object(storage, 'delete', side_effect=PermissionError(13, 'Permission denied', path)):
        delete_recording_completely(recording, storage=storage)
    db.session.commit()
    default_log = _assert_no_name(log)
    line = next(l for l in default_log.splitlines() if l.startswith(f"Error deleting media of recording {recording_id} "))
    assert LABEL.search(line) and "Permission denied" in line
    storage.delete(f"local://{os.path.relpath(path, app.config['UPLOAD_FOLDER'])}", missing_ok=True)


def test_retention_audio_removal_logs_the_stored_file_without_its_name(user, log):
    recording = _upload(user)
    log.clear()
    assert remove_recording_audio(recording, reason='retention') is True
    default_log = _assert_no_name(log)
    line = next(l for l in default_log.splitlines() if l.startswith(f"Removed audio file for recording {recording.id}:"))
    assert LABEL.search(line).group(1) == str(recording.id)


def test_retention_run_logs_no_file_name(user, log):
    """The whole retention run, in both deletion modes."""
    from datetime import datetime, timedelta
    import src.services.retention as retention
    for mode in ('audio_only', 'full_recording'):
        recording = _upload(user)
        recording.status = 'COMPLETED'
        recording.created_at = datetime.utcnow() - timedelta(days=400)
        db.session.commit()
        recording_id = recording.id
        log.clear()
        with patch.object(retention, 'ENABLE_AUTO_DELETION', True), \
             patch.object(retention, 'GLOBAL_RETENTION_DAYS', 30), \
             patch.object(retention, 'DELETION_MODE', mode):
            stats = retention.process_auto_deletion()
        assert 'error' not in stats, stats
        default_log = _assert_no_name(log)
        assert any(LABEL.search(l) and LABEL.search(l).group(1) == str(recording_id)
                   for l in default_log.splitlines()), default_log
        Recording.query.filter_by(user_id=user).delete(synchronize_session=False)
        db.session.commit()


def test_missing_media_logs_no_file_name(user, log):
    recording = _upload(user)
    get_storage_service().delete(recording.audio_path, missing_ok=True)
    log.clear()
    response = _client(user).get(f'/audio/{recording.id}')
    assert response.status_code == 404
    default_log = _assert_no_name(log)
    assert f"Audio file of recording {recording.id} missing from server: <stored file local://recordings/" in default_log


def test_loggable_locator():
    key = "recordings/2026/11/42/20261115101500_Jane_Doe_intake.MP3"
    assert loggable_locator(f"local://{key}") == "<stored file local://recordings/2026/11/42/20261115101500.mp3>"
    assert loggable_locator(f"s3://bucket/{key}") == "<stored file s3://bucket/recordings/2026/11/42/20261115101500.mp3>"
    assert loggable_locator(key) == "<stored file recordings/2026/11/42/20261115101500.mp3>"
    # A row of an earlier release: a path in the upload folder.
    legacy = "/data/uploads/20261115101500_0123456789abcdef0123456789abcdef_Jane_Doe.wav"
    assert loggable_locator(legacy) == "<stored file /data/uploads/20261115101500_0123456789ab.wav>"
    assert loggable_locator("/data/uploads/Jane Doe intake.m4a") == "<stored file /data/uploads/*.m4a>"
    assert loggable_locator("funasr/42/Jane_Doe.flac") == "<stored file funasr/42/*.flac>"
    assert loggable_locator("Jane Doe") == "<stored file *>"
    assert loggable_locator(None) == "<no file>"
    # The error text of the storage backend names the file as well.
    text = loggable_error(OSError(13, "Permission denied", f"/data/uploads/{key}"), f"local://{key}")
    assert "Jane" not in text and "Permission denied" in text and "/data/uploads/recordings/2026/11/42/" in text
