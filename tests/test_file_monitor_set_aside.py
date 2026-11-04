"""Watched files that cannot be processed, and watched files over the upload limits.

A watched file is processed like an upload, without the upload size limits. A
file that ingestion refuses is renamed to <name>.failed at once and its owner
is notified; any other failure is retried after a growing interval, and the
file is set aside after the fifth attempt. Later scans skip a .failed file.
"""
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from src.app import app, db
from src import file_monitor as fm_module
from src.api import recordings as recordings_api
from src.file_monitor import FileMonitor, MAX_ATTEMPTS
from src.models import Notification, Recording, User


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True)


def _audio(path, seconds=3):
    _ffmpeg('-f', 'lavfi', '-i', f'sine=duration={seconds}', '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '128k', path)


@pytest.fixture
def watch(monkeypatch):
    """A watch folder, a monitor and an owner; everything is removed afterwards."""
    monkeypatch.setenv('AUTO_PROCESS_STABILITY_TIME', '0')
    base = Path(tempfile.mkdtemp(prefix='watch-'))
    folder = base / 'inbox'
    folder.mkdir()
    with app.app_context():
        user = User(username='watch_' + uuid.uuid4().hex[:6], email=uuid.uuid4().hex[:8] + '@example.com',
                    password='x')
        db.session.add(user)
        db.session.commit()
        monitor = FileMonitor(str(base), 9999, 'admin_only')

        def scan():
            monitor._scan_directory_for_user(folder, user.id)
            db.session.expire_all()

        yield monitor, folder, user, scan
        for r in Recording.query.filter_by(user_id=user.id).all():
            db.session.delete(r)
        Notification.query.filter_by(user_id=user.id).delete()
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()
    shutil.rmtree(base, ignore_errors=True)


def _names(folder):
    return sorted(p.name for p in folder.iterdir())


def _notices(user):
    return Notification.query.filter_by(user_id=user.id, kind='auto_process.file_set_aside').all()


def test_a_watched_file_over_the_upload_limits_is_processed(watch):
    monitor, folder, user, scan = watch
    _audio(str(folder / 'long.mp3'), seconds=20)
    with patch.dict(app.config, {'MAX_CONTENT_LENGTH': 10_000}):
        scan()
    assert _names(folder) == []
    recs = Recording.query.filter_by(user_id=user.id, original_filename='long.mp3').all()
    assert len(recs) == 1 and recs[0].processing_source == 'auto_process'


def test_an_unreadable_file_is_set_aside_once_and_its_owner_told(watch):
    monitor, folder, user, scan = watch
    (folder / 'junk.mp3').write_bytes(os.urandom(50_000))
    with patch('src.api.recordings.ingest_uploaded_recording',
               wraps=recordings_api.ingest_uploaded_recording) as ingest:
        scan()
        scan()
    assert ingest.call_count == 1
    assert _names(folder) == ['junk.mp3.failed']
    assert Recording.query.filter_by(user_id=user.id).count() == 0
    notices = _notices(user)
    assert len(notices) == 1
    assert notices[0].message_key == 'notifications.autoProcessRefused'
    assert notices[0].params['filename'] == 'junk.mp3'
    assert notices[0].params['failed_name'] == 'junk.mp3.failed'
    assert notices[0].params['reason']


def test_a_video_without_audio_is_set_aside(watch):
    monitor, folder, user, scan = watch
    _ffmpeg('-f', 'lavfi', '-i', 'testsrc=duration=2:size=160x120:rate=10', '-c:v', 'mpeg4',
            str(folder / 'silent.mp4'))
    scan()
    assert _names(folder) == ['silent.mp4.failed']
    assert Recording.query.filter_by(user_id=user.id).count() == 0


def test_a_failed_name_already_in_use_is_not_overwritten(watch):
    monitor, folder, user, scan = watch
    (folder / 'junk.mp3.failed').write_bytes(b'earlier')
    (folder / 'junk.mp3').write_bytes(os.urandom(50_000))
    scan()
    names = _names(folder)
    assert 'junk.mp3.failed' in names and len(names) == 2
    assert (folder / 'junk.mp3.failed').read_bytes() == b'earlier'
    assert all(n.endswith('.mp3.failed') for n in names)


def test_other_errors_are_retried_later_and_set_aside_after_the_last_attempt(watch):
    monitor, folder, user, scan = watch
    _audio(str(folder / 'later.mp3'))
    key = str(folder / 'later.mp3')
    with patch('src.api.recordings.ingest_uploaded_recording', side_effect=RuntimeError('storage unavailable')) as ingest:
        scan()
        assert _names(folder) == ['later.mp3']
        assert monitor._failures[key][0] == 1
        scan()  # before the retry time: the file is not tried
        assert ingest.call_count == 1
        for attempt in range(2, MAX_ATTEMPTS + 1):
            monitor._failures[key] = (monitor._failures[key][0], time.time() - 1)
            scan()
            assert ingest.call_count == attempt
    assert _names(folder) == ['later.mp3.failed']
    assert key not in monitor._failures
    notices = _notices(user)
    assert len(notices) == 1 and notices[0].message_key == 'notifications.autoProcessFailed'


def test_the_retry_interval_grows(watch):
    monitor, folder, user, scan = watch
    _audio(str(folder / 'later.mp3'))
    key = str(folder / 'later.mp3')
    delays = []
    with patch('src.api.recordings.ingest_uploaded_recording', side_effect=RuntimeError('database down')):
        for _ in range(3):
            if key in monitor._failures:
                monitor._failures[key] = (monitor._failures[key][0], time.time() - 1)
            before = time.time()
            scan()
            delays.append(round(monitor._failures[key][1] - before))
    assert delays == [fm_module.RETRY_BASE_SECONDS, 2 * fm_module.RETRY_BASE_SECONDS, 4 * fm_module.RETRY_BASE_SECONDS]


def test_a_file_that_succeeds_after_a_failure_is_forgotten(watch):
    monitor, folder, user, scan = watch
    _audio(str(folder / 'later.mp3'))
    key = str(folder / 'later.mp3')
    with patch('src.api.recordings.ingest_uploaded_recording', side_effect=RuntimeError('database down')):
        scan()
    monitor._failures[key] = (1, time.time() - 1)
    scan()
    assert _names(folder) == []
    assert key not in monitor._failures
    assert Recording.query.filter_by(user_id=user.id, original_filename='later.mp3').count() == 1
