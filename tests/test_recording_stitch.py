"""Tests for the recording-stitch worker (#287 c/d).

Exercises the real ffmpeg concat-demux path (no mocking) on synthetic
audio. The container ships ffmpeg, so the end-to-end stitch is verified
by:

1. Generating two short sine-wave WAV chunks via ffmpeg.
2. Planting them as chunks in a real session dir.
3. Calling ``stitch_recording_session`` and checking the output file is
   playable + roughly the expected duration.

We use WAV instead of webm so we can deterministically generate inputs
without depending on a browser to produce the matroska clusters that
real MediaRecorder chunks have. The ``concat`` demuxer logic is
container-agnostic.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, RecordingSession
from src.services.recording_stitch import (
    stitch_recording_session,
    StitchError,
    _chunk_paths,
    _mime_to_extension,
)


app.config["WTF_CSRF_ENABLED"] = False


def _generate_wav(path, duration_seconds=1, freq_hz=440):
    """Generate a short sine-wave WAV via ffmpeg. Used as chunk fodder.

    We force the output format with -f wav because the chunk files use
    a generic .bin extension (matching the streaming-client convention),
    so ffmpeg would otherwise refuse to pick a muxer by guessing.
    """
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi',
        '-i', f'sine=frequency={freq_hz}:duration={duration_seconds}',
        '-ac', '1', '-ar', '16000',
        '-f', 'wav',
        path,
    ]
    subprocess.run(cmd, check=True)


def _probe_duration(path):
    """Return audio duration in seconds via ffprobe."""
    cmd = [
        'ffprobe', '-hide_banner', '-loglevel', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        path,
    ]
    out = subprocess.run(cmd, check=True, capture_output=True)
    return float(out.stdout.decode().strip())


def _make_user():
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"stitch_{suffix}",
        email=f"stitch_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_mime_to_extension_maps_known_types():
    assert _mime_to_extension('audio/webm') == 'webm'
    assert _mime_to_extension('audio/mp4') == 'm4a'
    assert _mime_to_extension('audio/ogg') == 'ogg'
    # Unknown → webm fallback (matches MediaRecorder default)
    assert _mime_to_extension('audio/whatever') == 'webm'
    assert _mime_to_extension('') == 'webm'


def test_chunk_paths_returns_sorted_chunks():
    tmp = tempfile.mkdtemp(prefix="speakr-stitch-")
    try:
        # Plant chunks out of name order
        for name in ('chunk-000003.bin', 'chunk-000001.bin', 'chunk-000002.bin'):
            with open(os.path.join(tmp, name), 'wb') as f:
                f.write(b'.')
        # Plant some garbage that should NOT be picked up
        with open(os.path.join(tmp, 'session.json'), 'wb') as f:
            f.write(b'{}')
        with open(os.path.join(tmp, 'chunk-bad.txt'), 'wb') as f:
            f.write(b'.')

        paths = _chunk_paths(tmp)
        assert [os.path.basename(p) for p in paths] == ['chunk-000001.bin', 'chunk-000002.bin', 'chunk-000003.bin']
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_chunk_paths_empty_dir_returns_empty_list():
    tmp = tempfile.mkdtemp(prefix="speakr-stitch-empty-")
    try:
        assert _chunk_paths(tmp) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stitch_session_concatenates_two_wav_chunks_end_to_end():
    """Plant two real WAV chunks; stitch produces a playable, ~2s WAV."""
    upload_folder = tempfile.mkdtemp(prefix="speakr-stitch-uploads-")
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _make_user()

        # Pre-create the placeholder Recording row, the way finalize_session does.
        recording = Recording(
            user_id=user.id,
            title="Stitch test",
            status='STITCHING',
            mime_type='audio/wav',
            processing_source='recording_session',
        )
        db.session.add(recording)
        db.session.flush()

        # Create the session row, pointing at the placeholder recording.
        session = RecordingSession(
            user_id=user.id,
            mime_type='audio/wav',
            status='finalizing',
            chunk_count=2,
            bytes_received=12345,
            finalized_recording_id=recording.id,
        )
        db.session.add(session)
        db.session.commit()

        sess_dir = os.path.join(upload_folder, "_sessions", session.id)
        os.makedirs(sess_dir, exist_ok=True)
        chunk_a = os.path.join(sess_dir, 'chunk-000001.bin')
        chunk_b = os.path.join(sess_dir, 'chunk-000002.bin')
        _generate_wav(chunk_a, duration_seconds=1, freq_hz=440)
        _generate_wav(chunk_b, duration_seconds=1, freq_hz=660)

        recording_id, audio_path, metadata = stitch_recording_session(session.id)

        # Output file exists and is playable
        assert os.path.exists(audio_path)
        duration = _probe_duration(audio_path)
        # Two 1-second chunks → roughly 2s total (ffmpeg may round a hair).
        assert 1.8 <= duration <= 2.2, f"unexpected duration {duration}"

        # Recording row was updated in place
        rec = db.session.get(Recording, recording_id)
        assert rec.status == 'PENDING'
        assert rec.audio_path == audio_path
        assert rec.file_size and rec.file_size > 0
        assert rec.original_filename

        # Session was marked finalized and its dir was removed
        sess = db.session.get(RecordingSession, session.id)
        assert sess.status == 'finalized'
        assert sess.finalized_at is not None
        assert not os.path.isdir(sess_dir)

        # Cleanup
        try:
            os.remove(audio_path)
        except OSError:
            pass
        db.session.delete(sess)
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_stitch_raises_when_no_chunks_on_disk():
    upload_folder = tempfile.mkdtemp(prefix="speakr-stitch-nochunks-")
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _make_user()
        recording = Recording(
            user_id=user.id,
            title="No chunks",
            status='STITCHING',
            mime_type='audio/webm',
            processing_source='recording_session',
        )
        db.session.add(recording)
        db.session.flush()
        session = RecordingSession(
            user_id=user.id,
            mime_type='audio/webm',
            status='finalizing',
            chunk_count=0,
            finalized_recording_id=recording.id,
        )
        db.session.add(session)
        db.session.commit()

        try:
            stitch_recording_session(session.id)
            assert False, "expected StitchError"
        except StitchError as e:
            assert 'no chunks' in str(e).lower()

        db.session.delete(session)
        db.session.delete(recording)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_stitch_raises_when_session_missing():
    with app.app_context():
        try:
            stitch_recording_session('00000000-0000-0000-0000-000000000000')
            assert False, "expected StitchError for missing session"
        except StitchError as e:
            assert 'not found' in str(e).lower()


_ORIGINAL_UPLOAD_FOLDER = app.config.get("UPLOAD_FOLDER")


def setup_function(function):  # noqa: D401 - pytest hook
    """Restore UPLOAD_FOLDER before each test in case a prior one mutated it."""
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER


def teardown_function(function):
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER


def teardown_module(module):
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER
    with app.app_context():
        for u in User.query.filter(User.username.like("stitch_%")).all():
            for s in RecordingSession.query.filter_by(user_id=u.id).all():
                db.session.delete(s)
            for r in Recording.query.filter_by(user_id=u.id).all():
                if r.audio_path and os.path.exists(r.audio_path):
                    try:
                        os.remove(r.audio_path)
                    except OSError:
                        pass
                db.session.delete(r)
            db.session.delete(u)
        db.session.commit()


if __name__ == "__main__":
    test_mime_to_extension_maps_known_types()
    test_chunk_paths_returns_sorted_chunks()
    test_chunk_paths_empty_dir_returns_empty_list()
    test_stitch_session_concatenates_two_wav_chunks_end_to_end()
    test_stitch_raises_when_no_chunks_on_disk()
    test_stitch_raises_when_session_missing()
    print("All stitch tests passed.")
