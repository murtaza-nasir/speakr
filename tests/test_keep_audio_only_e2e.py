"""End-to-end functional test for the keep_audio_only upload flow.

The earlier coverage in test_video_retention.py pins the source-code
shape (form-field parse, persistence assignment, processing-task
derivation) but not the runtime behaviour through the actual route.
This file does an HTTP POST through the upload handler with the form
field set and asserts the Recording row carries keep_audio_only=True.

The ffprobe / ffmpeg subprocess calls are mocked so the test can run
without a real video file.
"""
import io
import os
import sys
import uuid
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording

app.config["WTF_CSRF_ENABLED"] = False


def _setup_user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_upload_with_keep_audio_only_persists_flag_on_recording():
    """POST /upload with keep_audio_only=true sets recording.keep_audio_only
    in the DB. The processing pipeline reads this and discards the
    video stream; that derivation is covered by the static-analysis
    tests in test_video_retention.py."""
    with app.app_context():
        user = _setup_user('keep_e2e')
        client = app.test_client()
        _login(client, user)

        # Mock the codec probe to claim "video file" without invoking ffprobe.
        # Mock convert_if_needed so we don't actually shell out to ffmpeg.
        with patch('src.api.recordings.get_codec_info') as mock_probe, \
             patch('src.api.recordings.convert_if_needed') as mock_convert, \
             patch('src.services.job_queue.job_queue.enqueue', return_value=1):
            mock_probe.return_value = {
                'has_video': True,
                'audio_codec': 'aac',
                'video_codec': 'h264',
                'duration': 30.0,
            }
            # The storage layer now MOVES the converted file into the storage
            # root, so the mocked conversion must produce a real file on disk
            # (a fake path would FileNotFoundError on shutil.move). A real
            # production conversion always yields a real file, so this keeps the
            # test faithful to the post-storage upload flow.
            import tempfile
            conv_fd, conv_path = tempfile.mkstemp(suffix='.mp3')
            os.write(conv_fd, b'\x00' * (1024 * 1024))
            os.close(conv_fd)
            convert_result = MagicMock()
            convert_result.output_path = conv_path
            convert_result.was_converted = True
            convert_result.was_compressed = False
            convert_result.original_codec = 'aac'
            convert_result.final_codec = 'mp3'
            mock_convert.return_value = convert_result

            data = {
                'keep_audio_only': 'true',
                'title': 'e2e-test',
            }
            data['file'] = (io.BytesIO(b'\x00' * 16384), 'sample.mp4')
            resp = client.post('/upload', data=data, content_type='multipart/form-data')

        # Upload handler returns 202 on success; some failure modes
        # return other 2xx. Tolerate either as long as a Recording row
        # was created with the flag set.
        assert resp.status_code in (200, 202), (
            f'upload returned {resp.status_code}: {resp.data!r}'
        )

        rec = Recording.query.filter_by(user_id=user.id).order_by(Recording.id.desc()).first()
        assert rec is not None, 'upload did not create a Recording row'
        assert rec.keep_audio_only is True, (
            f'keep_audio_only form field set true, but Recording.keep_audio_only={rec.keep_audio_only}'
        )

        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()


def test_upload_without_keep_audio_only_leaves_flag_off_when_video_retention_on():
    """Mirror test for the negative case: with VIDEO_RETENTION=true at
    the server and no form-field override, the flag stays False so the
    processing pipeline keeps the video stream."""
    with app.app_context(), \
         patch('src.api.recordings.VIDEO_RETENTION', True):
        user = _setup_user('keep_e2e_off')
        client = app.test_client()
        _login(client, user)

        with patch('src.api.recordings.get_codec_info') as mock_probe, \
             patch('src.services.job_queue.job_queue.enqueue', return_value=1):
            mock_probe.return_value = {
                'has_video': True,
                'audio_codec': 'aac',
                'video_codec': 'h264',
                'duration': 30.0,
            }
            data = {'title': 'e2e-test-noflag'}
            data['file'] = (io.BytesIO(b'\x00' * 16384), 'sample.mp4')
            resp = client.post('/upload', data=data, content_type='multipart/form-data')

        assert resp.status_code in (200, 202), (
            f'upload returned {resp.status_code}: {resp.data!r}'
        )
        rec = Recording.query.filter_by(user_id=user.id).order_by(Recording.id.desc()).first()
        assert rec is not None
        assert rec.keep_audio_only is False, (
            f'No keep_audio_only and VR=true; Recording.keep_audio_only should '
            f'be False; got {rec.keep_audio_only}'
        )
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()
