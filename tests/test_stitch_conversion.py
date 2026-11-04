"""A stitched in-browser recording is converted like an upload (#495).

The chunks are stitched by stream copy, so the stitched file is still what the
browser recorded. Safari records Opus in 2.5 ms frames, which iOS and VLC play
back distorted, so an audio recording goes through the upload conversion
before it is transcribed. A video capture is kept as recorded.
"""
import os
import shutil
import subprocess
import tempfile
import uuid
from unittest.mock import patch

import pytest

from src.app import app, db
from src.models import Recording, RecordingSession, User
from src.services.recording_stitch import kickoff_transcription_for_stitched, stitch_recording_session
from src.services.storage import get_storage_service

app.config["WTF_CSRF_ENABLED"] = False


def _ffmpeg(*args):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args], check=True)


def _can_encode_webm():
    try:
        _ffmpeg('-f', 'lavfi', '-i', 'sine=duration=1', '-c:a', 'libopus', '-f', 'webm',
                os.path.join(tempfile.gettempdir(), f'_probe_{uuid.uuid4().hex}.webm'))
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(not _can_encode_webm(), reason="ffmpeg build lacks WebM/Opus encoder")


def _safari_like_webm(path, seconds=4):
    """WebM/Opus in 2.5 ms CELT frames, the shape of Safari's MediaRecorder output."""
    _ffmpeg('-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-ac', '1', '-ar', '48000',
            '-c:a', 'libopus', '-application', 'lowdelay', '-frame_duration', '2.5', '-f', 'webm', path)


def _video_webm(path, seconds=3):
    _ffmpeg('-f', 'lavfi', '-i', f'testsrc=duration={seconds}:size=160x120:rate=10',
            '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
            '-c:v', 'libvpx', '-c:a', 'libopus', '-ac', '1', '-ar', '48000', '-f', 'webm', path)


def _probe(path, entries):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', entries, '-of', 'csv=p=0', path],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def stitched():
    """Plant a recording session whose chunks are the given file, then stitch it."""
    upload_folder = tempfile.mkdtemp(prefix="speakr-stitch-convert-")
    created = []

    def make(writer, mime_type):
        app.config["UPLOAD_FOLDER"] = upload_folder
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f"stconv_{suffix}", email=f"stconv_{suffix}@local.test", password="x")
        db.session.add(user)
        db.session.flush()
        recording = Recording(user_id=user.id, title="Stitch convert", status='STITCHING',
                              mime_type=mime_type, processing_source='recording_session')
        db.session.add(recording)
        db.session.flush()
        session = RecordingSession(user_id=user.id, mime_type=mime_type, status='finalizing', chunk_count=1,
                                   bytes_received=1, finalized_recording_id=recording.id)
        db.session.add(session)
        db.session.commit()
        created.append((user.id, recording.id, session.id))
        sess_dir = os.path.join(upload_folder, "_sessions", session.id)
        os.makedirs(sess_dir)
        writer(os.path.join(sess_dir, 'chunk-000001.bin'))
        recording_id, _, metadata = stitch_recording_session(session.id)
        return db.session.get(Recording, recording_id), user, metadata

    with app.app_context():
        yield make
        storage = get_storage_service()
        for user_id, recording_id, session_id in created:
            rec = db.session.get(Recording, recording_id)
            if rec is not None:
                if rec.audio_path:
                    try:
                        storage.delete(rec.audio_path)
                    except Exception:
                        pass
                db.session.delete(rec)
            sess = db.session.get(RecordingSession, session_id)
            if sess is not None:
                db.session.delete(sess)
            db.session.delete(db.session.get(User, user_id))
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_a_stitched_audio_recording_gets_a_convert_job(stitched):
    rec, user, metadata = stitched(_safari_like_webm, 'audio/webm')
    with patch("src.services.job_queue.CONVERT_WORKERS", 1), \
         patch("src.services.job_queue.job_queue.enqueue") as enqueue:
        kickoff_transcription_for_stitched(recording_id=rec.id, user_id=user.id, metadata=metadata)
    enqueue.assert_called_once()
    kwargs = enqueue.call_args.kwargs
    assert kwargs['job_type'] == 'convert'
    assert kwargs['recording_id'] == rec.id
    assert 'transcribe' in kwargs['params']


def test_without_convert_workers_the_stitch_job_converts_the_file(stitched):
    rec, user, metadata = stitched(_safari_like_webm, 'audio/webm')
    stitched_path = rec.audio_path
    assert _probe(stitched_path, 'stream=codec_name') == 'opus'
    with patch("src.services.job_queue.CONVERT_WORKERS", 0), \
         patch("src.services.job_queue.job_queue.enqueue") as enqueue:
        kickoff_transcription_for_stitched(recording_id=rec.id, user_id=user.id, metadata=metadata)

    db.session.expire_all()
    rec = db.session.get(Recording, rec.id)
    assert rec.mime_type == 'audio/mpeg'
    assert rec.audio_path != stitched_path
    assert not os.path.exists(stitched_path)
    with get_storage_service().materialize(rec.audio_path) as stored:
        assert _probe(stored.local_path, 'stream=codec_name') == 'mp3'
        assert 3.5 <= float(_probe(stored.local_path, 'format=duration')) <= 4.5
    assert rec.file_size > 0
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs['job_type'] == 'transcribe'


def test_a_stitched_video_capture_is_kept_as_recorded(stitched):
    rec, user, metadata = stitched(_video_webm, 'video/webm')
    stitched_path = rec.audio_path
    with patch("src.services.job_queue.CONVERT_WORKERS", 1), \
         patch("src.services.job_queue.job_queue.enqueue") as enqueue:
        kickoff_transcription_for_stitched(recording_id=rec.id, user_id=user.id, metadata=metadata)
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs['job_type'] == 'transcribe'
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).audio_path == stitched_path
